"""The application behind the React page: what the page asks for, and what it is told when things change.

No Qt in here: everything runs on the event loop, the window-system parts (dialogs, window controls, thumbnails) come in
through a ``Platform`` object, and the page's side is a plain ``emit(event, payload)`` callback. ``ui/backend.py`` puts
this behind QWebChannel; the tests drive it directly with fakes.

Concepts
- A *conversation* is what one tab shows. Its controller is the engine's session (WittyCode's own loop, or Devin); when
  the user picks a model of the other engine the conversation gets a new controller that continues the same transcript,
  and the earlier text is handed over with the next message (``core/handoff``). The conversation id (``cid``) stays.
- Sidebar rows are saved sessions (native ids) plus open conversations (cids); a saved session that is open is shown once.

Calls (page -> here) are ``call(method, params)``; events (here -> page) are listed in ``frontend/src/protocol.ts``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import dataclasses
import itertools
import logging
import os
import stat
import subprocess
import sys
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from desktop.core.attachments import (
    HARD_ATTACHMENTS,
    HARD_TOTAL_BYTES,
    SOFT_ATTACHMENTS,
    SOFT_TOTAL_BYTES,
    Attachment,
    Rejection,
    Thumbnail,
    admit,
    describe_rejections,
    overage,
    too_many,
)
from desktop.core.connections import ConnectionError, ConnectionStore
from desktop.core.connector_auth import ConnectorStore
from desktop.core.plugin_http import PluginError
from desktop.core.skill_marketplace import SkillMarketplace
from desktop.core.cache import Cache
from desktop.core import storage_policy
from desktop.core import storage_meter
from desktop.core.conversation_history import ConversationHistory, prepend as prepend_history
from desktop.core.fusion.backends import GUARDED, PREFIXES
from desktop.core.catalog import API_PREFIX, Catalog, Target, build_catalog, settings_for_model
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.hooks import Hooks
from desktop.core.llm.backend import is_api_model
from desktop.core import files as filetools
from desktop.core.handoff import AS_TEXT, CarryOption, attachment_text, build_handoff, carry_options, carry_overage, conversation_messages
from desktop.core.harness import goal as goals
from desktop.core import customization, language, session_title, sounds
from desktop.core.harness import official_skill, skills
from desktop.core.import_bridge import ImportBridge
from desktop.core.chatgpt import side as chatgpt_side
from desktop.core.devin import side as devin_side
from desktop.core.memory.bridge import MemoryBridge, explain
from desktop.core.memory.curator import Curator, Exchange
from desktop.core.memory.review import migrate_proposals
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import MemoryRejected
from desktop.core.session_state import SessionLimitError, SessionState, eviction_message
from desktop.core.settings import (
    CHATGPT_PERMISSIONS,
    DEVIN_PERMISSIONS,
    FOLLOW_UPS,
    FONT_SCALES,
    LANGUAGES,
    COMPACT_AT_RANGE,
    MAX_OPEN_SESSIONS_RANGE,
    MAX_STEPS_RANGE,
    MAX_REMEMBERED_SESSIONS,
    MAX_SESSION_TITLE,
    MEMORY_ENGINES,
    RECONNECT_INTERVAL_RANGE,
    RECONNECT_LIMIT_RANGE,
    STEP_BUDGET_RANGE,
    SIDEBAR_WIDTH_RANGE,
    CONVERSATION_WIDTH_RANGE,
    valid_inspector_width,
    THEMES,
    AppSettings,
    default_settings_path,
    folder_key,
    normalize_folder,
    same_folder,
    save_settings,
)
from desktop.core.timefmt import format_relative
from desktop.core.sidebar_order import move_relative, pin_mirror
from desktop.core.transcript import Transcript, Turn, TurnStatus
from desktop.core.speed import speed_of
from desktop.core.usage import Tokens, UsageLedger
from desktop.core.viewmodel import attachment_json, context_json, turn_json, usage_json
from desktop.core.turn_delta import changed_blocks

log = logging.getLogger("desktop.app")

FLUSH_SECONDS = 0.04
SIDEBAR_SECONDS = 0.4
INDEX_SECONDS = 1.0
"""The cache's index (rows, models, accounts) is written this long after the last change."""
SNAPSHOT_SECONDS = 2.0
"""A session is written to the cache this long after it last changed (and when it closes)."""
MEMORY_POLL_SECONDS = 1.0
"""How often the application looks whether the shared memory was written (by an agent, through its own memory server)."""
QUEUE_SECONDS = 180.0
"""How long a message typed while a session connects waits for it."""
ACTIVITY_STEP = 10.0
"""A running turn updates its displayed activity age at most this often, in seconds. Activity never changes row order."""
PREFETCH_DEDUPE_SECONDS = 60.0
"""Repeat model selections and session READY events ask for the same profile at most this often per app run; the
records on disk dedupe the actual fetches."""
EVICTION_NOTICE = "已自动关闭最久未使用的会话"
RETRY_NOTICE = "这条消息没有发送成功：附件已放回输入框，可以直接重试。"
RETRY_OVER_NOTICE = "这条消息没有发送成功：附件已放回输入框。"
SESSION_LIST_LIMIT = None
"""List all sessions: an activity-based cutoff would still make manually ordered rows appear and disappear."""
DEVIN_ROW_PREFIX = "dv:"
"""Row ids of saved Devin sessions in the sidebar."""
CHATGPT_ROW_PREFIX = "cg:"
"""Row ids of saved conversations of WittyCode's own loop (ChatGPT Plan and the API providers) in the sidebar."""
ROW_PREFIXES = {"devin": DEVIN_ROW_PREFIX, "chatgpt": CHATGPT_ROW_PREFIX, "factory": "fy:"}
ENGINE_NAMES = {"devin": "Devin", "chatgpt": "ChatGPT Plan", "factory": "Factory"}
OWN_ENGINE = "chatgpt"
"""WittyCode's own agent loop: ChatGPT Plan's models and the API providers' (``core/chatgpt``, ``core/harness``)."""


def engine_label(engine: str, model: Mapping[str, Any] | None) -> str:
    """What the page calls the place a model runs: an API provider's model runs in WittyCode's own loop, but to the user
    it is "API" (the word on the model chip)."""
    source = str((model or {}).get("source") or "")
    return "API" if source.startswith(API_PREFIX) else ENGINE_NAMES.get(engine, engine)


def row_id(engine: str, native: str) -> str:
    """The sidebar row id of a saved session: the engine's own id behind the engine's prefix."""
    return ROW_PREFIXES.get(engine, "") + native
WINDOW_WAIT_STEPS = 20
WINDOW_WAIT_STEP = 0.05
"""How long (steps x seconds) an engine change waits for the new session to report its context window."""


class AppError(Exception):
    """A failure the user should read: the message is one Chinese sentence."""


class Platform(Protocol):
    """What only the window system can do."""

    async def pick_folder(self, start: str) -> str | None: ...

    async def pick_files(self, start: str) -> list[str]: ...

    async def pick_sound(self, start: str) -> str | None: ...

    def play_sound(self, path: str) -> None: ...

    def stop_sound(self) -> None: ...

    def open_url(self, url: str) -> None: ...

    def reveal(self, path: str) -> None: ...

    def open_path(self, path: str) -> None:
        """Open a file or folder with the program the system has for it."""
        ...

    def open_with(self, path: str) -> None:
        """The system's own "open with" choice for a file."""
        ...

    def show_in_folder(self, path: str) -> None:
        """Show a file selected in its folder."""
        ...

    def thumbnail(self, attachment: Attachment) -> Thumbnail | None: ...

    def load_bytes(self, name: str, data: bytes) -> Attachment | Rejection: ...

    def load_path(self, path: str) -> Attachment | Rejection: ...

    def full_picture(self, attachment: Attachment) -> str | None:
        """A ``data:`` URI of the whole picture, for the big view."""
        ...

    async def video_poster(self, path: filetools.InlineVideoSource, width: int) -> dict[str, Any]:
        ...

    async def video_open(self, path: str, width: int, publish: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        ...

    def video_control(self, action: str, params: dict[str, Any]) -> None:
        ...

    def video_close(self) -> None:
        ...

    def converted_picture(self, path: str) -> str:
        """A Qt-only picture as a PNG data URI, decoded off the event loop."""
        ...

    def pdf_pages(self, path: str) -> list[tuple[float, float]]:
        """The size in points of every page of a PDF, for the side panel. Raises ``files.FileProblem`` for one that cannot be drawn."""
        ...

    def pdf_page(self, path: str, page: int, width: int) -> str:
        """Page ``page`` (from 0) of a PDF drawn ``width`` pixels wide, as a ``data:`` URI. Raises ``files.FileProblem``."""
        ...

    def window_minimize(self) -> None: ...

    def window_toggle_maximize(self) -> bool: ...

    def window_close(self) -> None: ...

    def window_start_move(self) -> None: ...

    def window_start_resize(self, edge: str) -> None: ...

    def window_is_maximized(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    id: str
    text: str
    attachments: tuple[Attachment, ...] = ()
    raw_text: str | None = None


@dataclass(eq=False)
class Conversation:
    cid: str
    controller: Any
    project: str
    requested: dict[str, str] = field(default_factory=dict)
    """The settings asked for when the session was opened (what the page shows until the engine reports its own)."""
    hidden: set[str] = field(default_factory=set)
    """Native ids of earlier engines' sessions of this conversation: not listed again in the sidebar."""
    history: dict[str, Any] | None = None
    in_history: bool = False
    """An existing conversation or one with a retained first dispatch. Draft tabs are not project rows."""
    unsent_turns: set[str] = field(default_factory=set)
    draft_title: str = ""
    naming: bool = False
    sent_ids: list[str] = field(default_factory=list)
    sent_versions: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    drafts: list[Attachment] = field(default_factory=list)
    inflight: tuple[Any, str, tuple[Attachment, ...]] | None = None
    dirty: bool = True
    switching: bool = False
    """A change of engine is under way: the new controller failing to open must not close the conversation."""
    switch_task: asyncio.Task[Any] | None = None
    """The interruptible send-time handoff, including opening the destination."""
    preparing: QueuedMessage | None = None
    """A submitted message visible in the timeline while the source prepares its handoff, not yet a model turn."""
    pending_model: dict[str, Any] | None = None
    """A selection shown immediately, but not committed to the engine until the next user send."""
    created_at: float = field(default_factory=time.time)
    sidebar_key: str = ""
    """Stable sidebar identity, migrated to the native row id once it is known (also across engine changes)."""
    sidebar_active_at: float = 0.0
    """Ordering activity, unlike the displayed age, never advances on streaming updates."""
    activity_running: bool = False
    activity_state: SessionState | None = None
    sound_prompts: set[str] = field(default_factory=set)
    sound_complete_pending: bool = False
    sound_result: str = ""
    """A settled result waiting for the same publication that updates the session's status dot."""
    opening_row: SavedRow | None = None
    """Indexed display metadata until a saved conversation finishes opening; never used to configure the engine."""
    preview: bool = False
    """The page shows the cached conversation while the real history is still being loaded."""
    cached_turns: list[dict[str, Any]] = field(default_factory=list)
    cached_meta: dict[str, Any] = field(default_factory=dict)
    """Settings, token use and context size as they were when the session was last cached: shown until the live ones are known."""
    json_cache: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    cache_dirty: bool = False
    cache_handle: asyncio.TimerHandle | None = None
    queue: list[QueuedMessage] = field(default_factory=list)
    """Messages typed while the session was connecting or a turn was running: each goes out as a turn of its own, in
    order, once the one before it is over."""
    ran: bool = False
    """A turn was running since the queue was last looked at: when it ends, the queue is looked at again."""
    goal: goals.Goal | None = None
    """What the conversation works toward turn after turn. The own harness checkpoints it; reopening requires /goal resume."""
    curated: set[str] = field(default_factory=set)
    """Ids of the turns the memory curator was already shown (a turn is looked at once)."""
    ledger: UsageLedger = field(default_factory=UsageLedger)
    """The conversation's token total over all its sessions (see ``core/usage``)."""
    queue_task: asyncio.Future[Any] | None = None
    watching: str | None = None
    """The sub-agent whose run the page's side panel shows (its thread id): only that one is pushed to the page."""
    agent_sent_ids: list[str] = field(default_factory=list)
    agent_sent_versions: dict[str, int] = field(default_factory=dict)

    @property
    def engine(self) -> str:
        return getattr(self.controller, "engine", OWN_ENGINE)


@dataclass(frozen=True, slots=True)
class SavedRow:
    id: str
    native: str
    engine: str
    title: str
    modified: datetime
    count: int = 0
    model: str = ""
    """The page's id of the model, where the engine's list of conversations says which one it was on (the own loop's does)."""
    created: datetime | None = None
    fusion: bool = False


def _aware(stamp: datetime | None) -> datetime:
    if stamp is None:
        return datetime.fromtimestamp(0, timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def json_copy(value: Any) -> Any:
    """A copy that a thread can write out while the event loop goes on changing the original."""
    return copy.deepcopy(value)


def _row_to_cache(row: SavedRow) -> dict[str, Any]:
    return {"id": row.id, "native": row.native, "engine": row.engine, "title": row.title, "modified": row.modified.timestamp(), "count": row.count, "model": row.model, "created": row.created.timestamp() if row.created else None, "fusion": row.fusion}


def _row_from_cache(raw: Any) -> SavedRow | None:
    try:
        return SavedRow(
            str(raw["id"]),
            str(raw["native"]),
            str(raw["engine"]),
            str(raw["title"]),
            datetime.fromtimestamp(float(raw["modified"]), timezone.utc),
            int(raw.get("count") or 0),
            str(raw.get("model") or ""),
            datetime.fromtimestamp(float(raw["created"]), timezone.utc) if raw.get("created") else None,
            fusion=raw.get("fusion") is True,
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError, AttributeError):
        return None


def _is_fresh(snapshot: Mapping[str, Any], modified: float | None) -> bool:
    """The cached copy is as new as the session itself (one used elsewhere since would be a different conversation)."""
    try:
        return modified is None or float(snapshot.get("activeAt") or 0) >= modified - 5
    except (TypeError, ValueError):
        return False


def _is_complete(snapshot: Mapping[str, Any]) -> bool:
    """What a session shows from the cache: its conversation, its four settings, its token total and its context."""
    meta = snapshot.get("meta")
    if not snapshot.get("turns") or not isinstance(meta, Mapping):
        return False
    context = meta.get("context")
    return bool(meta.get("settings")) and bool(meta.get("usage")) and isinstance(context, Mapping) and context.get("used") is not None


class AppCore:
    def __init__(
        self,
        settings: AppSettings,
        platform: Platform,
        *,
        factory: Any = None,
        devin: Any = None,
        chatgpt: Any = None,
        providers: Any = None,
        persist: bool = True,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        offline: bool = False,
        cache: Cache | None = None,
        memory: MemoryLaunch | None = None,
    ) -> None:
        self.settings: AppSettings = settings
        self.platform = platform
        self.factory = factory
        self.devin = devin
        self.chatgpt = chatgpt
        if chatgpt is not None:
            chatgpt.factory = factory
            chatgpt.devin = devin
            chatgpt.archive_meter = self.archive_meter
            """The shared usage meter the session archives write through (``chatgpt/archive_binding``):
            the engines ask their manager for it, here the manager is this engine."""
        self.connections = ConnectionStore()
        self.connectors = ConnectorStore(self.connections.file.parent)
        self.skill_marketplace = SkillMarketplace(self.connections)
        self.history = ConversationHistory(default_settings_path().parent / "conversations")
        self._opening_history: dict[str, Any] | None = None
        self._opening_saved_row: str | None = None
        for engine in (factory, devin, chatgpt):
            if engine is not None:
                engine.connections = self.connections
        self.hooks = Hooks()
        """The application's own hooks (``core/hooks``), one set for every engine."""
        self.memory: MemoryBridge | None = None
        """The shared memory (``core/memory``), when the application has one: every engine's sessions are given it."""
        if memory is not None:
            self.memory = MemoryBridge(memory, row_id=row_id, review_mode=True)
            # the memory's hook is the first
            self.hooks.on_prompt(memory.prompt_hook)
            for engine in (factory, devin, chatgpt):
                if engine is not None and hasattr(engine, "memory"):
                    engine.memory = memory
        # Skills and custom commands (harness/skills): the same hook for every engine, with or without the memory.
        self.hooks.on_prompt(skills.prompt_hook)
        # The bundled numbered-reply skill (harness/official_skill): every engine's user-facing message, until the
        # settings switch turns it off; internal delegation prompts are left to their own protocol.
        self.hooks.on_prompt(official_skill.prompt_hook(lambda: self.settings.numbered_reply))
        # The language of the models' thinking (core/language): the own loop has it in its instructions.
        self.hooks.on_prompt(language.prompt_hook(lambda: self.settings.think_chinese, lambda: self.settings.answer_language, lambda: self.settings.thinking_language))
        self.hooks.on_prompt(customization.prompt_hook)
        for engine in (factory, devin, chatgpt):
            if engine is not None and hasattr(engine, "hooks"):
                engine.hooks = self.hooks
        self.curator: Curator | None = None
        """Asks, after a turn, what is worth keeping and writes what is sure enough into the memory itself; the user looks
        at the week's notes once, on the memory page (``core/memory/review``)."""
        if self.memory is not None:
            self.curator = Curator(
                self.memory.store, self.memory.proposals, self._extract_for_memory,
                review=self.memory.review,
                learner=self.memory.learner,
                enabled=lambda engine: (
                    self.settings.memory_enabled
                    and self.settings.memory_curate
                    and engine in self.settings.memory_engines
                    and self._can_ask(engine)
                ),
                changed=self._suggestions_changed,
            )
        self.imports: ImportBridge | None = None
        """What other agents keep (``core/importers``): notes for the shared memory, conversations for ChatGPT Plan."""
        if self.memory is not None:
            self.imports = ImportBridge(
                self.memory.store,
                getattr(chatgpt, "sessions", None),
                row=lambda native: CHATGPT_ROW_PREFIX + native,
                defaults=self._import_defaults,
            )
        self._memory_state: tuple[int, int] | None = None
        self._memory_handle: asyncio.TimerHandle | None = None
        self._runs_handle: asyncio.TimerHandle | None = None
        self.providers = providers
        self.persist = persist
        self.offline = offline
        self.cache = cache if cache is not None else Cache(None)
        self._archive_meter: storage_meter.ArchiveMeter | None = None
        self._index: dict[str, Any] = {}
        self._index_handle: asyncio.TimerHandle | None = None
        self._hydrated = False
        self._preview: dict[str, Any] | None = None
        self._windows: dict[str, int] = {}
        """Context window of each model (page model id) as a live session last reported it: kept in the cache so that
        a session shown from the cache, or read without being opened, has its ``Context 38K/872K`` at once."""
        page = emit or (lambda event, payload: None)

        def emit_and_remember(event: str, payload: dict[str, Any]) -> None:
            if event == "accounts":
                self._remember_accounts(payload)
            page(event, payload)

        self.emit: Callable[[str, dict[str, Any]], None] = emit_and_remember

        self.catalog = Catalog()
        self.order: list[Conversation] = []
        self._by_controller: dict[Any, Conversation] = {}
        self._by_cid: dict[str, Conversation] = {}
        # closed or replaced controllers, whose late events are ignored; weak, or every closed conversation (its whole
        # transcript, pictures included) would stay in memory for as long as the app runs
        self._retired: weakref.WeakSet[Any] = weakref.WeakSet()
        self._cids = itertools.count(1)
        self._draft_ids = itertools.count(1)
        self._queue_ids = itertools.count(1)
        self._ledger_seed: dict[str, Any] | None = None
        self._adopt: Conversation | None = None
        self._select_next = False
        self._opening_meta: dict[str, str] | None = None
        self._saved: dict[str, list[SavedRow]] = {}
        # row id -> when a session closed during this run last had a message. An engine may stamp a saved session with
        # the start of its turn, sometimes the end, so a row that has just closed would otherwise move from the time shown
        # while it was open.
        self._spoken: dict[str, float] = {}
        self._tasks: set[asyncio.Future[Any]] = set()
        self._profiles_source: Any = None
        """The model-profile fetch (tests substitute a fake; the real one only ever asks Artificial Analysis)."""
        self._prefetched_profiles: dict[str, float] = {}  # model -> monotonic of last spawn
        self._flush_handle: asyncio.TimerHandle | None = None
        self._sidebar_handle: asyncio.TimerHandle | None = None
        self._sidebar_reload: set[str] = set()
        # per attachment object, not per id: every transcript numbers its attachments from the same start, so two
        # conversations have an "a3" each; and an entry goes when its attachment does (a removed draft, a closed tab)
        self._thumbs: weakref.WeakKeyDictionary[Attachment, Thumbnail | None] = weakref.WeakKeyDictionary()
        self._models_loaded = False
        self._shutting_down = False
        self._video_preview: tuple[str, str] | None = None
        self._inline_video_posters = asyncio.Lock()
        self._inline_images = asyncio.Semaphore(2)

        if factory is not None:
            factory.add_listener(self._on_event)
        if devin is not None:
            devin.add_listener(self._on_event)
            devin.add_account_listener(self._on_devin_account)
        if chatgpt is not None:
            chatgpt.add_listener(self._on_event)
            chatgpt.add_account_listener(self._on_chatgpt_account)

    # ------------------------------------------------------------------ plumbing

    def _spawn(self, coroutine: Awaitable[Any], *, quiet: bool = False) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)

        def done(finished: asyncio.Future[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                log.error("background task failed", exc_info=error)
                if not quiet:
                    self.say(f"操作失败：{error_text(error)}")

        task.add_done_callback(done)
        return task

    def say(self, text: str) -> None:
        self.emit("notice", {"text": text})

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        handler = getattr(self, "_m_" + method.replace(".", "_"), None)
        if handler is None:
            raise AppError(f"没有这个操作：{method}")
        return await handler(dict(params or {}))

    # ------------------------------------------------------------------ settings and payloads

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            save_settings(self.settings)
        except OSError as error:
            log.warning("could not save settings: %s", error)
            self.say(f"无法保存设置：{error}")

    def _levels_of(self, engine: str) -> tuple[str, ...]:
        """The permission scale of an engine."""
        if engine == "factory":
            return ("off", "low", "medium", "high")
        return DEVIN_PERMISSIONS if engine == "devin" else CHATGPT_PERMISSIONS

    def _autonomy_of(self, engine: str) -> str:
        """The permission level new sessions of an engine start with."""
        s = self.settings
        if engine == "factory":
            return s.factory_autonomy
        return s.devin_autonomy if engine == "devin" else s.chatgpt_autonomy

    def _engine_defaults(self, page_model: str) -> dict[str, str]:
        engine = self.catalog.engine_of(page_model)
        autonomy = self._autonomy_of(engine)
        return {
            "model": page_model,
            "reasoning": self.settings.reasoning_effort,
            "autonomy": autonomy,
            "mode": self.settings.mode,
        }

    def defaults(self) -> dict[str, str]:
        model = self.settings.default_model
        hidden = set(self.settings.hidden_models)
        if model and self.catalog.models and (self.catalog.find(model) is None or model in hidden):
            # removed (an API provider), of an engine that is not available now, or hidden by the user from the model
            # menu: not a model to start with (the setting stays, so unhiding it brings it back)
            model = ""
        if not model:
            # the first model of whatever there is, in the menu's order
            first = next((m for m in self.catalog.models if not m.get("disabled") and m["id"] not in hidden), None)
            model = first["id"] if first else ""
        return self._engine_defaults(model)

    def settings_view(self) -> dict[str, Any]:
        return {
            "maxOpen": self.settings.max_open_sessions,
            "devinPath": self.settings.devin_path,
            "hiddenModels": list(self.settings.hidden_models),
            "memoryEnabled": self.settings.memory_enabled,
            "memoryRecall": self.settings.memory_recall,
            "memoryCapture": self.settings.memory_capture,
            "memoryCurate": self.settings.memory_curate,
            "memoryEngines": list(self.settings.memory_engines),
            "theme": self.settings.theme,
            "fontScale": self.settings.font_scale,
            "followUp": self.settings.follow_up,
            "sidebarWidth": self.settings.sidebar_width,
            "conversationWidth": self.settings.conversation_width,
            "inspectorWidth": self.settings.inspector_width,
            "lastProject": self.settings.last_project,
            "compactAuto": self.settings.chatgpt_compact,
            "compactAt": self.settings.chatgpt_compact_at,
            "maxSteps": self.settings.max_steps,
            "reconnectInterval": self.settings.reconnect_interval,
            "reconnectLimit": self.settings.reconnect_limit,
            "fusionMain": self.settings.fusion_main,
            "fusionWorkers": list(self.settings.fusion_workers),
            "fusionLight": self.settings.fusion_light,
            "fusionReasoning": dict(self.settings.fusion_reasoning),
            "fusionPreference": self.settings.fusion_preference,
            "fusionMaxCalls": self.settings.fusion_max_calls,
            "fusionMaxTokens": self.settings.fusion_max_tokens,
            "fusionMaxSeconds": self.settings.fusion_max_seconds,
            "fusionMaxSecondsAdaptive": self.settings.fusion_max_seconds_adaptive,
            "fusionStepsMin": self.settings.fusion_steps_min,
            "fusionStepsMinAdaptive": self.settings.fusion_steps_min_adaptive,
            "fusionStepsMax": self.settings.fusion_steps_max,
            "fusionStepsMaxAdaptive": self.settings.fusion_steps_max_adaptive,
            "thinkChinese": self.settings.think_chinese,
            "answerLanguage": self.settings.answer_language,
            "thinkingLanguage": self.settings.thinking_language or ("zh-CN" if self.settings.think_chinese else "auto"),
            "numberedReply": self.settings.numbered_reply,
            "soundEnabled": self.settings.sound_enabled,
            "soundQuestion": sounds.sound_path("question", self.settings.sound_question),
            "soundComplete": sounds.sound_path("complete", self.settings.sound_complete),
            "soundError": sounds.sound_path("error", self.settings.sound_error),
        }

    def _storage_policy(self) -> storage_policy.StoragePolicy:
        return storage_policy.StoragePolicy(
            self.settings.storage_max_bytes,
            dict(self.settings.storage_shares),
            self.settings.storage_alert_at,
            self.settings.storage_full_policy,
            tuple(self.settings.storage_autoclean),
        )

    @staticmethod
    def _managed_file_bytes(path: Path) -> int:
        try:
            info = path.lstat()
        except OSError:
            return 0
        return info.st_size if stat.S_ISREG(info.st_mode) else 0

    def session_archive_root(self) -> Path:
        """The session archive root: ``<application data>/session`` (final spec §1) — beside the own
        engine's root, the same folder ``ArchiveBinding`` opens (``manager.root.parent / 'session'``),
        so the meter measures exactly what the bindings write. Without the own engine it stays beside
        the settings file. Read-only to call."""
        root = getattr(self.chatgpt, "root", None)
        if root is not None:
            return Path(root).parent / "session"
        return default_settings_path().parent / "session"

    def archive_meter(self) -> storage_meter.ArchiveMeter:
        """The shared usage meter for the session archive — the object ``SessionArchive`` takes as
        ``meter`` so its writes are gated and counted. The gate reads live settings, so a changed
        policy applies to the next write, not the next launch."""
        if self._archive_meter is None:
            self._archive_meter = storage_meter.ArchiveMeter(self.session_archive_root(), gate=self._archive_gate)
        return self._archive_meter

    def _archive_gate(self, category: str, incoming: int,
                      archive_usage: dict[str, int | None]) -> dict[str, Any]:
        """The verdict the meter consults inside its serialized transaction: archive-side usage merged
        with the application's own cache and shared memory index, judged by the current policy."""
        usage = self._storage_usage(archive_usage=archive_usage)
        return storage_policy.check_write(self._storage_policy(), usage, category, incoming)

    def _storage_usage(self, archive_usage: dict[str, int | None] | None = None) -> dict[str, int | None]:
        """Occupancy the application itself can state, per managed-data category; anything it does not
        measure stays unknown — the page must never draw it as zero (``storage_policy.report``).

        ``cache`` is the application's own cache folder (index.json and sessions/*.json) — ``None``
        means there is no managed cache at all, a known zero, not an unmeasured figure; ``index``
        the shared memory's rebuildable SQLite index plus the session archive's ``index.sqlite``
        and WAL sidecars; ``history`` and ``dump`` are the session archive's metered bytes, per the
        usage ledger — real counts for archive-managed writes only. Legacy history outside the
        archive root is an explicit boundary: never scanned, never silently counted as zero."""
        usage: dict[str, int | None] = {name: None for name in storage_policy.CATEGORIES}
        if self.cache.enabled and self.cache.folder is not None:
            total = sum(self._managed_file_bytes(path) for path in (self.cache.folder / "sessions").glob("*.json"))
            usage["cache"] = total + self._managed_file_bytes(self.cache.folder / "index.json")
        elif self.cache.folder is None:
            usage["cache"] = 0
        archive = archive_usage if archive_usage is not None else self.archive_meter().usage()
        usage["history"], usage["dump"] = archive["history"], archive["dump"]
        index_bytes = archive["index"] or 0
        if self.memory is not None:
            index = self.memory.store.index_path
            index_bytes += sum(self._managed_file_bytes(index.with_name(index.name + suffix))
                               for suffix in ("", "-wal", "-shm", "-journal"))
        usage["index"] = index_bytes
        return usage

    def storage_view(self) -> dict[str, Any]:
        """The Storage page: the split and cap, real occupancy where the application measures it,
        and the metered archive's scope — root, boundary, unmetered entries, cleanup support."""
        view = storage_policy.report(self._storage_policy(), self._storage_usage())
        view["managed"] = {"archive": self.archive_meter().describe()}
        return view

    def check_capacity(self, category: str, incoming: int) -> dict[str, Any]:
        """Pre-write gate for the archive layer — see ``storage_policy.check_write`` for the verdict shape."""
        return storage_policy.check_write(self._storage_policy(), self._storage_usage(), category, incoming)

    def project_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": folder,
                "name": Path(folder).name or folder,
                "path": folder,
                "collapsed": False,
                "sessionIds": [row["id"] for row in self._rows_for(folder)],
            }
            for folder in self.settings.projects
        ]

    # ------------------------------------------------------------------ sidebar

    def _resolve_row(self, row: str) -> str:
        seen = set()
        while row in self.settings.session_links and row not in seen:
            seen.add(row)
            row = self.settings.session_links[row]
        return row

    def _visible_saved(self, folder: str) -> list[SavedRow]:
        return [row for row in self._saved.get(normalize_folder(folder), []) if self._resolve_row(row.id) == row.id]

    def _mirror_pins(self, folder: str, *, add: str = "", drop: str = "") -> None:
        """Keep this project's legacy ``session_pins`` a mirror of the global pin list, in its order."""
        key = folder_key(folder)
        known = {row["key"] for row in self._rows_for(folder) if row["pinned"]} | set(self.settings.session_pins.get(key, ()))
        if add:
            known.add(add)
        known.discard(drop)
        mirror = pin_mirror(self.settings.pinned_sessions, known)
        if mirror:
            self.settings.session_pins[key] = mirror
        else:
            self.settings.session_pins.pop(key, None)

    def _rows_for(self, folder: str) -> list[dict[str, Any]]:
        open_here = [c for c in self.order if same_folder(c.project, folder)]
        taken: set[str] = set()
        for conv in open_here:
            taken |= conv.hidden
            taken.add(conv.sidebar_key)
            native = getattr(conv.controller, "session_id", None)
            if native:
                taken.add(native)
                taken.update(prefix + native for prefix in ROW_PREFIXES.values())
        rows: list[tuple[str, float, str]] = []
        for saved in self._visible_saved(folder):
            if saved.native in taken or saved.id in taken or saved.id in self.settings.archived_sessions:
                continue
            rows.append((saved.id, saved.modified.timestamp(), saved.id))
        for conv in open_here:
            # A titled first draft already has its row: the first dispatch decides whether it stays or goes again.
            if (conv.in_history or conv.draft_title) and conv.sidebar_key not in self.settings.archived_sessions:
                rows.append((conv.sidebar_key or self._row_of(conv) or conv.cid, conv.sidebar_active_at, conv.cid))
        pins = {key: i for i, key in enumerate(self.settings.pinned_sessions)}
        ranks = {key: i for i, key in enumerate(self.settings.session_order)}

        def rank(row: tuple[str, float, str]) -> tuple[int, float, str]:
            key, active, _ = row
            if key in pins:
                return (0, pins[key], key)
            if key in ranks:
                return (2, ranks[key], key)
            return (1, -active, key)

        rows.sort(key=rank)
        return [{"id": ident, "key": key, "pinned": key in pins} for key, _, ident in rows]

    def _sync_sidebar_key(self, conv: Conversation) -> None:
        if conv.switching or not conv.in_history:
            return  # neither a draft nor a tentative engine replacement has a persistent sidebar identity
        key = self._row_of(conv) or conv.sidebar_key or conv.cid
        if key == conv.sidebar_key:
            return
        previous = conv.sidebar_key
        conv.sidebar_key = key
        if previous in self.settings.session_order:
            self.settings.session_order = list(dict.fromkeys(key if item == previous else item for item in self.settings.session_order))
        if previous in self.settings.pinned_sessions:
            self.settings.pinned_sessions = list(dict.fromkeys(key if item == previous else item for item in self.settings.pinned_sessions))
        for mirror in self.settings.session_pins.values():
            if previous in mirror:
                mirror[:] = dict.fromkeys(key if item == previous else item for item in mirror)
        if previous:
            self.settings.session_created.pop(previous, None)
        self.settings.session_created[key] = conv.created_at
        self._save()

    def _admit_history(self, conv: Conversation) -> None:
        if conv.in_history or conv.switching:
            return
        conv.in_history = True
        self._sync_sidebar_key(conv)
        if conv.draft_title:
            self._remember_auto_title(conv, conv.sidebar_key, conv.draft_title)
            conv.draft_title = ""
        self._promote_session(conv)
        self._schedule_sidebar(conv.project, reload=True)

    def _remember_session_order(self, keys: list[str]) -> None:
        known = set(keys)
        self.settings.session_order = keys + [item for item in self.settings.session_order if item not in known]

    def _promote_session(self, conv: Conversation) -> None:
        """Move to the front of ordinary rows after significant activity; pinned order only changes by user action."""
        conv.sidebar_active_at = conv.controller.active_at
        keys = [row["key"] for row in self._rows_for(conv.project) if not row["pinned"]]
        if conv.sidebar_key not in keys:
            return
        keys.remove(conv.sidebar_key)
        self._remember_session_order([conv.sidebar_key, *keys])
        self._save()
        self._schedule_sidebar(conv.project)

    def _sidebar_project(self, folder: Any) -> str:
        if isinstance(folder, str) and folder.strip() and "\0" not in folder:
            try:
                key = folder_key(folder)
                match = next((item for item in self.settings.projects if folder_key(item) == key), None)
                if match is not None:
                    return match
            except (OSError, RuntimeError, ValueError):
                pass
        raise AppError("找不到这个项目。")

    async def _m_session_pin(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = self._sidebar_project(params.get("projectId"))
        ident, pinned = params.get("id"), params.get("pinned")
        if not isinstance(ident, str) or not isinstance(pinned, bool):
            raise AppError("置顶参数不合法。")
        rows = self._rows_for(folder)
        row = next((item for item in rows if item["id"] == ident), None)
        if row is None or not row["key"].startswith(tuple(ROW_PREFIXES.values())):
            raise AppError("只能置顶本项目列表中已保存的会话。")
        if row["pinned"] == pinned:
            return {"ok": True}
        key, pins = row["key"], self.settings.pinned_sessions
        if pinned:
            if len(pins) >= MAX_REMEMBERED_SESSIONS:
                raise AppError("置顶会话已达到上限。")
            pins.insert(0, key)  # the newest pin leads the zone
            self._mirror_pins(folder, add=key)
        else:
            pins.remove(key)
            self._mirror_pins(folder, drop=key)
            self._remember_session_order([key, *(item["key"] for item in rows if not item["pinned"])])
        self._save()
        if ident in self._by_cid:
            self.emit("session", {"id": ident, "patch": {"pinned": pinned}})
        self._emit_sidebar()
        return {"ok": True}

    async def _m_session_reorder(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = self._sidebar_project(params.get("projectId"))
        ident, target, after = params.get("id"), params.get("targetId"), params.get("after", False)
        if not isinstance(ident, str) or not isinstance(target, str) or not isinstance(after, bool):
            raise AppError("会话顺序不合法。")
        rows = self._rows_for(folder)
        by_id = {row["id"]: row for row in rows}
        source = by_id.get(ident)
        if source is None:
            raise AppError("只能拖动仍在列表中的会话。")
        if ident == target:
            return {"ok": True}
        if source["pinned"]:
            found = next(
                ((other, row) for other in self.settings.projects for row in self._rows_for(other) if row["id"] == target), None
            )
            if found is None or not found[1]["pinned"]:
                raise AppError("只能在同一置顶分区内拖动会话。")
            move_relative(self.settings.pinned_sessions, source["key"], found[1]["key"], after)
            self._mirror_pins(folder)
            if not same_folder(found[0], folder):
                self._mirror_pins(found[0])
        else:
            target_row = by_id.get(target)
            if target_row is None or target_row["pinned"]:
                raise AppError("只能在同一个项目内拖动仍在列表中的会话。")
            keys = [row["key"] for row in rows if not row["pinned"]]
            move_relative(keys, source["key"], target_row["key"], after)
            self._remember_session_order(keys)
        self._save()
        self._emit_sidebar()
        return {"ok": True}

    def _saved_json(self, saved: SavedRow, folder: str, *, pinned: bool) -> dict[str, Any]:
        return {
            "id": saved.id,
            "projectId": folder,
            "title": self.settings.session_titles.get(saved.id) or self.settings.session_auto_titles.get(saved.id) or saved.title or "（无标题）",
            "modified": format_relative(saved.modified),
            "modifiedAt": int(saved.modified.timestamp() * 1000),
            "engine": saved.engine,
            "fusion": saved.fusion,
            "pinned": pinned,
            "settings": {"model": saved.model, "reasoning": "", "autonomy": "", "mode": "auto"},
            "usage": None,
            "context": {"used": None, "window": 0, "model": "", "compacted": False},
            "workingLabel": "",
        }

    def _saved_sessions_json(self) -> dict[str, Any]:
        sessions = {}
        pins = set(self.settings.pinned_sessions)
        for folder in self.settings.projects:
            for saved in self._visible_saved(folder):
                if saved.id not in self.settings.archived_sessions:
                    sessions[saved.id] = self._saved_json(saved, folder, pinned=saved.id in pins)
        return sessions

    def _archived_json(self) -> list[dict[str, Any]]:
        """The archived conversations the engines still list, the one archived last first."""
        found: list[tuple[int, dict[str, Any]]] = []
        pins = set(self.settings.pinned_sessions)
        for folder in self.settings.projects:
            for saved in self._visible_saved(folder):
                stamp = self.settings.archived_sessions.get(saved.id)
                if stamp is not None:
                    found.append((stamp, self._saved_json(saved, folder, pinned=saved.id in pins)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in found]

    def _pinned_ids(self) -> list[str]:
        """The pinned rows' public ids (an open conversation's cid), in the zone's order; hidden ones stay out."""
        if not self.settings.pinned_sessions:
            return []
        rank = {key: i for i, key in enumerate(self.settings.pinned_sessions)}
        rows = [row for folder in self.settings.projects for row in self._rows_for(folder) if row["key"] in rank]
        rows.sort(key=lambda row: rank[row["key"]])
        return [row["id"] for row in rows]

    def _emit_sidebar(self) -> None:
        self.emit("sidebar", {"projects": self.project_rows(), "pinnedSessionIds": self._pinned_ids(), "sessions": self._saved_sessions_json(), "archived": self._archived_json()})

    def _schedule_sidebar(self, folder: str | Path, *, reload: bool = False) -> None:
        if not any(same_folder(folder, item) for item in self.settings.projects):
            return
        if reload:
            self._sidebar_reload.add(normalize_folder(folder))
        if self._sidebar_handle is None and not self._shutting_down:
            self._sidebar_handle = asyncio.get_running_loop().call_later(SIDEBAR_SECONDS, self._flush_sidebar)

    def _flush_sidebar(self) -> None:
        self._sidebar_handle = None
        if self._shutting_down:
            return
        reload, self._sidebar_reload = self._sidebar_reload, set()
        for key in reload:
            folder = next((item for item in self.settings.projects if normalize_folder(item) == key), None)
            if folder is not None:
                self._spawn(self._reload_project(folder), quiet=True)
        self._emit_sidebar()

    async def _reload_project(self, folder: str) -> None:
        rows: list[SavedRow] = []
        if self.factory is not None and self.settings.factory_enabled:
            with contextlib.suppress(Exception):
                for item in await self.factory.list_sessions(folder, SESSION_LIST_LIMIT):
                    rows.append(SavedRow("fy:" + item.id, item.id, "factory", item.title or "", item.modified_at, item.message_count or 0, self.catalog.page_id("factory", item.model), item.created_at))
        if self.devin is not None and self.settings.devin_enabled:
            with contextlib.suppress(Exception):
                for item in await self.devin.list_sessions(folder, SESSION_LIST_LIMIT):
                    devin_row = DEVIN_ROW_PREFIX + item.id
                    live = next((conv for conv in self.order if self._row_of(conv) == devin_row), None)
                    if live is not None and not live.in_history:
                        if not getattr(item, "has_user_messages", False):
                            continue  # older ACPs can omit the flag; allocation alone says nothing about a live draft
                        self._admit_history(live)
                    rows.append(SavedRow(devin_row, item.id, "devin", item.title or "", self._latest(devin_row, item.modified_at), item.message_count or 0, created=_aware(item.created_at)))
        own_loop = self.settings.chatgpt_enabled or self._has_api_models()
        guarded = (self.devin is not None and self.settings.devin_enabled or self.factory is not None and self.settings.factory_enabled)
        if self.chatgpt is not None and (own_loop or guarded):
            with contextlib.suppress(Exception):
                for item in await self.chatgpt.list_sessions(folder, SESSION_LIST_LIMIT):
                    if not own_loop and not item.fusion:
                        continue  # A native Fusion seat uses the host's archive, not its standalone CLI index.
                    chatgpt_row = CHATGPT_ROW_PREFIX + item.id
                    stamp = datetime.fromtimestamp(item.modified_at, timezone.utc)
                    page_model = self.catalog.page_id("chatgpt", item.model) if item.model and self.catalog is not None else ""
                    created = datetime.fromtimestamp(item.created_at, timezone.utc)
                    rows.append(SavedRow(chatgpt_row, item.id, "chatgpt", item.title or "", self._latest(chatgpt_row, stamp), item.messages, page_model, created, fusion=item.fusion))
        # A just-switched segment may have no native messages yet, or its CLI may currently be unavailable.
        for head in set(self.settings.session_links.values()):
            if self._resolve_row(head) != head or any(row.id == head for row in rows):
                continue
            try:
                saved = await asyncio.to_thread(self.history.load, head)
                if saved and same_folder(saved["project"], folder):
                    engine, native = head.split(":", 1)
                    rows.append(SavedRow(head, native, {"cg": "chatgpt", "dv": "devin", "fy": "factory"}[engine], saved["title"], datetime.fromtimestamp(saved["activeAt"], timezone.utc), created=datetime.fromtimestamp(saved["created"], timezone.utc)))
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("a conversation segment could not be listed")
            if not any(row.id == head for row in rows):
                earlier = next((row for row in rows if self._resolve_row(row.id) == head), None)
                if earlier is not None:
                    prefix, native = head.split(":", 1)
                    rows.append(dataclasses.replace(earlier, id=head, native=native, engine={"cg": "chatgpt", "dv": "devin", "fy": "factory"}[prefix], model="", fusion=False))
        self._saved[normalize_folder(folder)] = rows
        self._remember_rows(folder, rows)
        self._schedule_sidebar(folder)

    # ------------------------------------------------------------------ models and accounts

    async def _load_models(self, *, startup: bool = False) -> None:
        async def from_factory() -> list[Any]:
            if self.factory is None or not self.settings.factory_enabled:
                return []
            with contextlib.suppress(Exception):
                return list(await self.factory.list_models())
            return []

        async def from_devin() -> list[Any]:
            try:
                # The startup account event and catalog must use the same sign-in check.
                account = await self._refresh_accounts() if startup else None
                if self.devin is None or not self.settings.devin_enabled:
                    return []
                if account is None:
                    account = await self.devin.account()
                if account.get("auth") == "signed-in":
                    # app.init also runs on page reload, when the engine may have an old in-memory catalog.
                    return list(await self.devin.list_models(refresh=startup))
            except Exception as error:
                log.warning("could not refresh the Devin models: %s", error)
            return []

        async def from_chatgpt() -> list[Any]:
            if self.chatgpt is None or not self.settings.chatgpt_enabled:
                return []
            with contextlib.suppress(Exception):
                account = await self.chatgpt.account()
                if account.get("auth") == "signed-in" and account.get("planEnabled"):
                    return list(await self.chatgpt.list_models())
            return []

        async def from_providers() -> list[Any]:
            """The API providers' models: read from the application's own file, which takes no program to start. They are
            run by the application's own loop, so without it (and without a provider store) there are none."""
            lister = getattr(self.providers, "api_models", None)
            if self.chatgpt is None or lister is None:
                return []
            try:
                return list(await asyncio.to_thread(lister))
            except Exception as error:
                log.warning("could not read the API providers' models: %s", error)
                return []

        # Some take seconds (the Devin account and its process): asked together, not one after the other.
        factory_models, devin_models, chatgpt_models, api_models = await asyncio.gather(from_factory(), from_devin(), from_chatgpt(), from_providers())
        provider_of = getattr(self.providers, "provider_of", None)
        window_of = getattr(self.providers, "window_of", None)
        self.catalog = build_catalog(
            factory_models=factory_models,
            devin_models=devin_models,
            chatgpt_models=chatgpt_models,
            api_models=api_models,
            provider_of=provider_of,
            window_of=window_of,
        )
        self._models_loaded = True
        self._remember_models()
        self.emit("models", {"models": self.catalog.models, "defaults": self.defaults()})
        for conv in self.order:
            conv.dirty = True
        self._schedule_flush()

    def _schedule_models(self, *, startup: bool = False) -> None:
        self._spawn(self._load_models(startup=startup), quiet=True)

    def _on_devin_account(self, account: Mapping[str, Any]) -> None:
        """The engine noticed a change of the account (a sign-in finished, a sign-out)."""
        if self._shutting_down:
            return
        self.emit("accounts", {"devin": {**account, "enabled": self.settings.devin_enabled}})

    async def _devin_account(self) -> dict[str, Any]:
        if self.devin is None:
            data: dict[str, Any] = {"cli": "missing", "auth": "signed-out"}
        else:
            try:
                data = dict(await self.devin.account())
            except Exception as error:
                log.warning("devin account failed: %s", error)
                data = {"cli": "found", "auth": "signed-out"}
        data["enabled"] = self.settings.devin_enabled
        return data

    def _on_chatgpt_account(self, account: Mapping[str, Any]) -> None:
        """The engine noticed a change of the account (a sign-in finished or failed, a sign-out, a refresh that was refused)."""
        if self._shutting_down:
            return
        self.emit("accounts", {"chatgpt": self._chatgpt_view(account)})

    def _chatgpt_view(self, account: Mapping[str, Any]) -> dict[str, Any]:
        return {**account, "enabled": self.settings.chatgpt_enabled, "welcomed": self.settings.chatgpt_welcomed}

    def _chatgpt_data(self) -> dict[str, Any]:
        """What the engine knows of the account, from the credential files (no network)."""
        try:
            return dict(self.chatgpt.cached_account())
        except Exception as error:
            log.warning("chatgpt account failed: %s", error)
            return {"auth": "signed-out", "email": "", "planEnabled": False, "accounts": []}

    def _chatgpt_now(self) -> dict[str, Any]:
        """The ChatGPT account for the page (the page has it with ``app.init``, before anything is connected)."""
        if self.chatgpt is None:
            return {"auth": "signed-out", "email": "", "planEnabled": False, "accounts": [], "available": False, "enabled": False, "welcomed": True}
        return {**self._chatgpt_view(self._chatgpt_data()), "available": True}

    async def _chatgpt_account(self) -> dict[str, Any]:
        if self.chatgpt is None:
            return self._chatgpt_now()
        data = await asyncio.to_thread(self._chatgpt_data)
        return {**self._chatgpt_view(data), "available": True}  # (the settings are read here, after the wait)

    def _provider_rows(self) -> list[dict[str, Any]]:
        if self.providers is None:
            return []
        try:
            return list(self.providers.list())
        except Exception as error:  # e.g. an unreadable providers file: say so, keep the rest of the app
            log.warning("could not read the API providers: %s", error)
            self.say(f"读取 API Provider 失败：{error_text(error)}")
            return []

    async def _refresh_accounts(self) -> dict[str, Any]:
        """Publish the accounts once the page is up and return the Devin status for model discovery."""
        devin = await self._devin_account()
        # Read when it is sent, not while the slow one was awaited: the user may have dismissed the one-time note or
        # signed in meanwhile, and a late event with the old values would bring it all back.
        self.emit("accounts", {"devin": devin, "chatgpt": self._chatgpt_now()})
        return devin

    # ------------------------------------------------------------------ the cache

    def _hydrate(self) -> None:
        """What was last known (rows, models, accounts), before anything is connected. A missing cache is nothing."""
        if self._hydrated:
            return
        self._hydrated = True
        self._index = {key: value for key, value in self.cache.load_index().items() if key != "v"}
        rows = self._index.get("rows")
        for folder, items in (rows.items() if isinstance(rows, dict) else ()):
            if not any(same_folder(folder, project) for project in self.settings.projects):
                continue
            self._saved[normalize_folder(folder)] = [row for row in map(_row_from_cache, items if isinstance(items, list) else ()) if row]
        models = self._index.get("models")
        if isinstance(models, list) and not self.catalog.models:
            items = [m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]
            if items:
                self.catalog = Catalog.from_items(items)
        windows = self._index.get("windows")
        if isinstance(windows, dict):
            self._windows = {str(k): int(v) for k, v in windows.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}

    def _schedule_index(self) -> None:
        if not self.cache.enabled or self._index_handle is not None or self._shutting_down:
            return
        try:
            self._index_handle = asyncio.get_running_loop().call_later(INDEX_SECONDS, self._write_index)
        except RuntimeError:  # no loop running (a synchronous caller): nothing to write later
            return

    def _write_index(self) -> None:
        self._index_handle = None
        if self._shutting_down:
            return
        self._spawn(asyncio.to_thread(self.cache.save_index, json_copy(self._index)), quiet=True)

    def _remember_accounts(self, payload: Mapping[str, Any]) -> None:
        devin = payload.get("devin")
        if isinstance(devin, Mapping) and devin.get("auth") in ("signed-in", "signed-out"):
            self._index["devin"] = {key: devin[key] for key in ("cli", "auth", "email", "plan") if key in devin}
        self._schedule_index()

    def _remember_rows(self, folder: str, rows: Sequence[SavedRow]) -> None:
        self._index.setdefault("rows", {})[normalize_folder(folder)] = [_row_to_cache(row) for row in rows]
        self._schedule_index()

    def _remember_models(self) -> None:
        if self.catalog.models:  # a failed load must not wipe the list that was good
            self._index["models"] = self.catalog.models
            self._schedule_index()

    def _learn_window(self, conv: Conversation, model_id: str) -> None:
        """Note the context window a connected session reports for its model (a different model served in its place
        says nothing about this one)."""
        context = conv.controller.context
        window = getattr(context, "window", None)
        if not model_id or not window or window <= 0 or self._windows.get(model_id) == window:
            return
        served = getattr(context, "model", "")
        if served and self.catalog.page_id(conv.engine, served) != model_id:
            return
        self._windows[model_id] = int(window)
        self._index["windows"] = self._windows
        self._schedule_index()

    def _learn_window_from(self, snapshot: Mapping[str, Any]) -> None:
        """A window a cached session was last seen with, for a model no live session has reported on in this cache yet."""
        meta = snapshot.get("meta")
        meta = meta if isinstance(meta, Mapping) else {}
        context, settings = meta.get("context"), meta.get("settings")
        if not isinstance(context, Mapping) or not isinstance(settings, Mapping):
            return
        window, model_id = context.get("window"), settings.get("model")
        if not model_id or model_id in self._windows or isinstance(window, bool) or not isinstance(window, (int, float)) or window <= 0:
            return
        served = context.get("model")
        if served and self.catalog.page_id(str(snapshot.get("engine") or OWN_ENGINE), str(served)) != model_id:
            return
        self._windows[str(model_id)] = int(window)
        self._index["windows"] = self._windows
        self._schedule_index()

    def _cached_devin(self) -> dict[str, Any] | None:
        cached = self._index.get("devin")
        if self.devin is None or not isinstance(cached, dict) or cached.get("auth") not in ("signed-in", "signed-out"):
            return None
        return {**cached, "cli": "found", "enabled": self.settings.devin_enabled}

    def _snapshot(self, conv: Conversation) -> tuple[str, dict[str, Any]] | None:
        """One conversation as the page shows it, for the cache (``None``: nothing worth keeping)."""
        controller = conv.controller
        native = getattr(controller, "session_id", None)
        turns = [turn for turn in controller.transcript.turns if turn.id not in conv.unsent_turns]
        if not conv.in_history or not native or not turns or conv.preview or not getattr(controller, "_history_loaded", True):
            return None  # a failed/incomplete load must never overwrite the last usable local history
        listed = []
        for turn in turns:
            entry = conv.json_cache.get(turn.id)
            if entry is None or entry[0] != turn.version:
                entry = (turn.version, turn_json(turn, self._thumb))
                conv.json_cache[turn.id] = entry
            listed.append(entry[1])
        meta = self._session_meta(conv)
        return self._snapshot_data(
            conv.engine,
            native,
            conv.project,
            self._title_of(conv),
            controller.active_at,
            listed,
            {name: meta[name] for name in ("settings", "usage", "usageModels", "speed", "context")},
            conv.ledger,
        )

    @staticmethod
    def _snapshot_data(
        engine: str,
        native: str,
        project: str,
        title: str,
        active_at: float,
        turns: list[dict[str, Any]],
        meta: dict[str, Any],
        ledger: UsageLedger,
    ) -> tuple[str, dict[str, Any]]:
        return row_id(engine, native), {
            "engine": engine,
            "native": native,
            "project": project,
            "title": title,
            "activeAt": active_at,
            "turns": turns,
            "meta": meta,
            "ledger": ledger.to_json(),
        }

    def _mark_cache_dirty(self, conv: Conversation) -> None:
        if not self.cache.enabled:
            return
        conv.cache_dirty = True
        if conv.cache_handle is None and not self._shutting_down and conv.controller.state in (SessionState.READY, SessionState.RUNNING, SessionState.DEAD):
            conv.cache_handle = asyncio.get_running_loop().call_later(SNAPSHOT_SECONDS, self._write_snapshot, conv)

    def _write_snapshot(self, conv: Conversation) -> None:
        conv.cache_handle = None
        if not conv.cache_dirty or self._shutting_down:
            return
        built = self._snapshot(conv)
        conv.cache_dirty = False
        if built is not None:
            self._spawn(asyncio.to_thread(self.cache.save_session, *built), quiet=True)

    def _write_snapshots_now(self) -> None:
        """At the end: whatever has not been written yet, written here and now."""
        for conv in self.order:
            if conv.cache_handle is not None:
                conv.cache_handle.cancel()
                conv.cache_handle = None
            if conv.cache_dirty:
                built = self._snapshot(conv)
                conv.cache_dirty = False
                if built is not None:
                    self.cache.save_session(*built)
        if self._index_handle is not None:
            self._index_handle.cancel()
            self._index_handle = None
        if self.cache.enabled and self._index:
            self.cache.save_index(json_copy(self._index))

    def _show_preview(self, conv: Conversation, snapshot: Mapping[str, Any]) -> None:
        """The conversation as it was last time, at once; the real history replaces it when the engine has loaded it."""
        conv.preview = True
        conv.cached_turns = list(snapshot["turns"])
        conv.cached_meta = dict(snapshot.get("meta") or {})
        conv.sent_ids = ["preview"]  # whatever is sent next replaces the list instead of adding to it
        patch = {
            "id": conv.cid,
            "projectId": conv.project,
            "mark": self._mark(conv),
            "engine": conv.engine,
            "title": snapshot.get("title") or "",
            **conv.cached_meta,
            "settings": self._shown_settings(conv),
            "fusion": self._shown_fusion(conv),
            "pinned": conv.sidebar_key in self.settings.pinned_sessions,
        }
        self.emit("session", {"id": conv.cid, "patch": patch})
        self.emit("turns", {"id": conv.cid, "turns": list(snapshot["turns"]), "all": True})

    # ------------------------------------------------------------------ init

    async def _m_app_init(self, params: dict[str, Any]) -> dict[str, Any]:
        self._close_video_preview()  # reloading the page may skip the old player's React cleanup
        first_init = not self._hydrated
        self._hydrate()
        if first_init and not self.offline:
            self._spawn(self._refresh_profiles(), quiet=True)
        for conv in self.order:  # a page that has just (re)started knows nothing of the open conversations yet
            conv.meta, conv.sent_ids, conv.sent_versions, conv.dirty = {}, [], {}, True
        self._schedule_flush()
        for folder in list(self.settings.projects):
            self._spawn(self._reload_project(folder), quiet=True)
        self._schedule_models(startup=True)
        devin = self._cached_devin() or {
            "cli": "found" if self.devin is not None else "missing",
            "auth": "checking" if self.devin is not None else "signed-out",
            "enabled": self.settings.devin_enabled,
        }
        memory = None
        if self.memory is not None:
            await asyncio.to_thread(self._migrate_suggestions)
            memory = await asyncio.to_thread(self.memory.state)
            if self._memory_handle is None and not self._shutting_down:
                self._memory_state = self.memory.store.version()
                self._memory_handle = asyncio.get_running_loop().call_later(MEMORY_POLL_SECONDS, self._watch_memory)
        return {
            "version": 1,
            "memory": memory,
            "projects": self.project_rows(),
            "pinnedSessionIds": self._pinned_ids(),
            "sessions": self._saved_sessions_json(),
            "archived": self._archived_json(),
            "tabs": [c.cid for c in self.order],
            "models": self.catalog.models,
            "defaults": self.defaults(),
            "settings": self.settings_view(),
            "devin": devin,
            "chatgpt": self._chatgpt_now(),
            "providers": self._provider_rows(),
            "maximized": self.platform.window_is_maximized(),
            "offline": self.offline,
        }

    # ------------------------------------------------------------------ conversations: events from the engines

    def _conv_of(self, controller: Any) -> Conversation | None:
        return self._by_controller.get(controller)

    def _on_event(self, controller: Any, kind: str) -> None:
        if self._shutting_down or controller in self._retired:
            return
        try:
            self._handle_event(controller, kind)
        except Exception:
            log.exception("handling %s of a session failed", kind)

    def _handle_event(self, controller: Any, kind: str) -> None:
        conv = self._conv_of(controller)
        if kind == "evicted":
            if conv is not None:
                self.say(controller.eviction_notice)
            return
        if conv is None:
            if controller.state is SessionState.CLOSED:
                return
            conv = self._adopt_or_create(controller)
        if controller.state is SessionState.CLOSED:
            self._drop(conv, controller)
            return
        if controller.state is not SessionState.OPENING:
            conv.opening_row = None
        if not conv.in_history and not conv.switching:
            turns = controller.transcript.turns
            turn = turns[-1] if turns else None
            # The own loop knows when the first request has a durable history. For native clients, use
            # conversation output or a completed user prompt, not merely RUNNING or a local error block.
            ready = getattr(controller, "history_ready", None)
            if ready is None:
                ready = turn is not None and turn.user_block is not None and (
                    turn.status is TurnStatus.DONE or any(b.kind in ("text", "thinking", "tool") for b in turn.blocks)
                )
            if ready:
                self._admit_history(conv)
            elif turn is not None and turn.status in (TurnStatus.FAILED, TurnStatus.INTERRUPTED):
                conv.unsent_turns.add(turn.id)
                self._discard_draft_title(conv)
        self._sync_sidebar_key(conv)
        if conv.history and not conv.switching and controller.state in (SessionState.READY, SessionState.RUNNING) and getattr(controller, "_history_loaded", True):
            prefix = conv.history["turns"]
            if prefix and prefix[0] not in controller.transcript.turns:
                if not controller.transcript.turns:
                    controller.handoff = conv.history.get("handoff")
                prepend_history(controller.transcript, prefix, self._row_of(conv))
        conv.dirty = True
        self._session_activity(conv)
        if kind == "fusion_title":
            self._remember_auto_title(conv, self._row_of(conv), controller.title)
        elif kind == "state":
            self._schedule_sidebar(conv.project, reload=controller.state is SessionState.READY)
            if controller.state is SessionState.READY:
                self._prefetch_conv(conv)
            if conv.preview and controller.state is SessionState.DEAD:
                self.say(f"{controller.last_error}；仍显示本地缓存，可以重新连接。")
        elif controller.state is SessionState.RUNNING and time.time() - controller.active_at >= ACTIVITY_STEP:
            # A long turn is conversation too: keep its displayed activity age current without changing its position.
            controller.active_at = time.time()
            self._schedule_sidebar(conv.project)
        if kind == "steer":
            self._return_steers(conv)
        if controller.state is SessionState.RUNNING:
            conv.ran = True
        elif conv.queue or conv.ran:
            asyncio.get_running_loop().call_soon(self._follow_queue, conv)
        self._schedule_flush()

    def _play_notification(self, kind: str) -> None:
        if self.offline or not self.settings.sound_enabled:
            return
        try:
            path = sounds.validate_path(sounds.sound_path(kind, getattr(self.settings, "sound_" + kind)))
            self.platform.play_sound(path)
        except (sounds.SoundProblem, OSError):
            self.say("提示音无法播放，请在设置 → 会话中检查声音路径。")

    def _session_activity(self, conv: Conversation) -> None:
        controller = conv.controller
        state = controller.state
        running = state is SessionState.RUNNING
        if running and not conv.activity_running:
            conv.sound_prompts.clear()
            conv.sound_complete_pending = False
            conv.sound_result = ""
            self._promote_session(conv)
        elif conv.activity_running and state in (SessionState.READY, SessionState.DEAD):
            turns = controller.transcript.turns
            status = turns[-1].status if turns else None
            if status in (TurnStatus.DONE, TurnStatus.FAILED):
                self._promote_session(conv)
                if status is TurnStatus.DONE and state is SessionState.READY:
                    plan = getattr(getattr(controller, "saved", None), "plan", {})
                    if isinstance(plan, dict) and plan.get("status") == "pending":
                        self._play_notification("question")
                    else:
                        conv.sound_complete_pending = True
                else:
                    conv.sound_result = "error"
            elif state is SessionState.DEAD:
                conv.sound_result = "error"
        elif state is SessionState.DEAD and conv.activity_state is not state:
            conv.sound_result = "error"
        conv.activity_running, conv.activity_state = running, state

    def _notify_session(self, conv: Conversation, mark: str) -> None:
        """Publish sounds with the status dot, never from intermediate engine events."""
        result, conv.sound_result = conv.sound_result, ""
        if self._shutting_down or conv.switching:
            return
        if mark == "attention":
            ids = {p.id for p in self._attention_prompts(conv)}
            if ids - conv.sound_prompts:
                conv.sound_prompts.update(ids)
                self._play_notification("question")
        elif mark == "ready" and result == "complete":
            self._play_notification("complete")
        elif mark in ("ready", "dead") and result == "error":
            self._play_notification("error")

    def _notify_complete(self, conv: Conversation, previous_goal: goals.Goal | None) -> None:
        """Stage completion only for the settled conversation, never a tool step or an intermediate goal/queued turn."""
        pending, conv.sound_complete_pending = conv.sound_complete_pending, False
        controller = conv.controller
        if not pending or self._shutting_down or conv.switching or controller.state is not SessionState.READY or conv.queue or conv.goal is not None:
            return
        if controller.transcript.active_turn is not None or getattr(controller, "stop_hook_blocked", False) or getattr(controller, "_save_failed", False):
            return
        turns = controller.transcript.turns
        turn = turns[-1] if turns else None
        if turn is None or turn.status is not TurnStatus.DONE:
            return
        if previous_goal is not None:
            texts = [block.text for block in turn.blocks if block.kind == "text" and block.text]
            if goals.mark_of(texts[-1] if texts else "")[0] != "done":
                return
        if conv.engine == OWN_ENGINE:
            records = getattr(getattr(controller, "saved", None), "turns", ())
            if not records or records[-1].status != "done":
                return
        elif conv.engine == "devin":
            stop = getattr(controller, "_stop", None)
            if stop and stop.get("stopReason", "end_turn") != "end_turn":
                return
        conv.sound_result = "complete"
        conv.dirty = True
        self._schedule_flush()

    def _adopt_or_create(self, controller: Any) -> Conversation:
        if self._adopt is not None:
            conv, self._adopt = self._adopt, None
            old = conv.controller
            self._by_controller.pop(old, None)
            self._retired.add(old)
            # An engine change is not a message: the conversation keeps its place in the sidebar.
            controller.active_at = old.active_at
            conv.watching = None
            conv.controller = controller
            self._by_controller[controller] = conv
            return conv
        conv = Conversation(
            cid=f"c{next(self._cids)}",
            controller=controller,
            project=normalize_folder(controller.project),
            requested=dict(self._opening_meta or {}),
            history=self._opening_history,
            sidebar_active_at=controller.active_at,
        )
        if conv.history and self._ledger_seed is None:
            conv.ledger = UsageLedger.from_json(conv.history["ledger"])
        key = self._row_of(conv)
        saved = next((row for row in self._saved.get(conv.project, []) if row.id == key), None)
        conv.in_history = bool(key and (key == self._opening_saved_row or saved is not None))
        if controller.state is SessionState.OPENING:
            conv.opening_row = saved
        conv.created_at = self.settings.session_created.get(key) or (
            _aware(saved.created).timestamp() if saved is not None and saved.created else conv.created_at
        )
        self._sync_sidebar_key(conv)
        if self._ledger_seed is not None:
            conv.ledger = self._ledger_of(self._ledger_seed)
        self.order.append(conv)
        self._by_controller[controller] = conv
        self._by_cid[conv.cid] = conv
        self.emit("tabs", {"tabs": [c.cid for c in self.order], "select": conv.cid if self._select_next else None})
        self._select_next = False
        self._emit_sidebar()
        if self._preview is not None:
            snapshot, self._preview = self._preview, None
            self._show_preview(conv, snapshot)
        return conv

    def _drop(self, conv: Conversation, controller: Any) -> None:
        if conv.controller is not controller or conv.switching:
            return
        if conv.queue:  # the tab goes with the session: the messages must not go with it unsaid
            if len(conv.queue) == 1:
                self.say(f"没有连上，这条消息没有发出：{conv.queue[0].text or '（只有附件）'}")
            else:
                self.say("没有连上，这些消息没有发出：" + "；".join(m.text or "（只有附件）" for m in conv.queue))
            conv.queue = []
        self._remove(conv)
        self._schedule_sidebar(conv.project, reload=True)

    def _latest(self, row_id: str, listed: datetime | None) -> datetime:
        """The later of the time the engine lists for a saved session and when it last had a message here."""
        listed_at = _aware(listed)
        spoken = self._spoken.get(row_id)
        return max(listed_at, datetime.fromtimestamp(spoken, timezone.utc)) if spoken else listed_at

    def _remember(self, conv: Conversation) -> None:
        controller = conv.controller
        native = getattr(controller, "session_id", None)
        active = getattr(controller, "active_at", None)
        if conv.in_history and native and active:
            row = row_id(conv.engine, native)
            self._spoken[row] = max(self._spoken.get(row, 0.0), active)

    def _remove(self, conv: Conversation) -> None:
        if self._video_preview is not None and self._video_preview[0] == conv.cid:
            self._close_video_preview()
        self._remember(conv)
        if conv.cache_handle is not None:
            conv.cache_handle.cancel()
            conv.cache_handle = None
        if conv.queue_task is not None:
            conv.queue_task.cancel()
        if conv.cache_dirty and conv.controller.state is not SessionState.RUNNING and not self._shutting_down:
            built = self._snapshot(conv)
            conv.cache_dirty = False
            if built is not None:
                self._spawn(asyncio.to_thread(self.cache.save_session, *built), quiet=True)
        if conv in self.order:
            self.order.remove(conv)
        self._by_controller.pop(conv.controller, None)
        self._by_cid.pop(conv.cid, None)
        self.emit("tabs", {"tabs": [c.cid for c in self.order], "select": None})
        self._emit_sidebar()

    # ------------------------------------------------------------------ conversations: what the page is told

    def _schedule_flush(self) -> None:
        if self._flush_handle is None and not self._shutting_down:
            self._flush_handle = asyncio.get_running_loop().call_later(FLUSH_SECONDS, self._flush_all)

    def _flush_all(self) -> None:
        self._flush_handle = None
        if self._shutting_down:
            return
        for conv in list(self.order):
            if conv.dirty:
                try:
                    self._flush(conv)
                except Exception:
                    log.exception("sending a session to the page failed")

    def _thumb(self, attachment: Attachment) -> Thumbnail | None:
        if attachment not in self._thumbs:
            self._thumbs[attachment] = self.platform.thumbnail(attachment)
        return self._thumbs[attachment]

    def _settings_of(self, engine: str, live: Any) -> dict[str, str]:
        return {
            "model": self.catalog.page_id(engine, live.model),
            "reasoning": live.reasoning,
            "autonomy": live.autonomy,
            "mode": live.mode or "auto",
        }

    def _page_settings(self, conv: Conversation) -> dict[str, str]:
        live = conv.controller.live_settings() if hasattr(conv.controller, "live_settings") else None
        if live is not None and conv.controller.state in (SessionState.READY, SessionState.RUNNING):
            return self._settings_of(conv.engine, live)
        return dict(conv.requested)

    def _shown_settings(self, conv: Conversation) -> dict[str, str]:
        """The engine's settings, overlaid by a selection waiting for the next explicit send."""
        if conv.pending_model is not None:
            return dict(conv.pending_model["settings"])
        live = conv.controller.live_settings() if hasattr(conv.controller, "live_settings") else None
        if live is not None and conv.controller.state in (SessionState.READY, SessionState.RUNNING):
            return self._settings_of(conv.engine, live)
        saved = getattr(conv.controller, "saved_settings", None)
        left = self._settings_of(conv.engine, saved) if saved is not None else dict(conv.cached_meta.get("settings") or {})
        if saved is None and conv.opening_row is not None and conv.opening_row.model:
            left["model"] = conv.opening_row.model
        return {**left, **conv.requested}

    @staticmethod
    def _shown_fusion(conv: Conversation) -> bool:
        if conv.pending_model is not None:
            return conv.pending_model["model"] == "fusion"
        if getattr(conv.controller, "_fusion", None) is not None:
            return True
        return conv.controller.state is SessionState.OPENING and conv.opening_row is not None and conv.opening_row.fusion

    def _attention_prompts(self, conv: Conversation) -> list[Any]:
        controller = conv.controller
        return [*controller.transcript.pending_prompts(), *getattr(controller, "agent_prompts", lambda: [])()]

    def _mark(self, conv: Conversation) -> str:
        controller = conv.controller
        state = controller.state
        if conv.switch_task is not None:
            return "running"
        if state is SessionState.RUNNING:
            return "attention" if self._attention_prompts(conv) else "running"
        return {SessionState.READY: "ready", SessionState.OPENING: "opening", SessionState.DEAD: "dead"}.get(state, "ready")

    def _feed_usage(self, conv: Conversation) -> None:
        """Add what the engine has counted since last time to the conversation's total."""
        if conv.switching:  # (the figures belong to two sessions at once; ``_switch_engine`` settles the books itself)
            return
        controller = conv.controller
        series_of = getattr(controller, "usage_series", None)
        if series_of is not None:
            series = series_of()
        else:  # (the offline replay's controller)
            counted = Tokens.of(getattr(controller, "usage", None))
            series = {} if counted is None else {"self": counted}
        conv.ledger.feed(series)
        models_of = getattr(controller, "usage_model_series", None)
        if models_of is not None:
            conv.ledger.feed_models({
                key: (self.catalog.page_id(conv.engine, model), tokens)
                for key, (model, tokens) in models_of().items()
            })

    def _row_of(self, conv: Conversation) -> str:
        """The sidebar row of an open conversation ("" until the engine has given it an id of its own)."""
        native = getattr(conv.controller, "session_id", None)
        return row_id(conv.engine, native) if native else ""

    def _title_of(self, conv: Conversation) -> str:
        """The name the user gave the conversation here, else the engine's title."""
        row = self._resolve_row(self._row_of(conv))
        return (self.settings.session_titles.get(row) or conv.draft_title
                or self.settings.session_auto_titles.get(row) or conv.controller.title)

    @staticmethod
    def _cache_fits(live: Mapping[str, Any], cached: Mapping[str, Any]) -> bool:
        """Whether the context figures kept in the cache still describe the conversation: they are the stand-in only until the
        live session has measured one of its own, and never for a conversation that was compacted since (the live ``compacted``
        is the word of the session, checked before) or one that moved to another model or window."""
        for key in ("model", "window"):
            if live.get(key) and cached.get(key) and live[key] != cached[key]:
                return False
        return True

    def _session_meta(self, conv: Conversation) -> dict[str, Any]:
        controller = conv.controller
        cached = conv.cached_meta
        settings = self._shown_settings(conv)
        model_id = settings.get("model", "")
        model = self.catalog.find(model_id)
        handoff = getattr(controller, "handoff", None)
        plan = getattr(getattr(controller, "saved", None), "plan", {})
        pending_plan = ({"id": plan["id"], "text": plan["text"]}
                        if isinstance(plan, dict) and plan.get("status") == "pending" and settings.get("mode") == "spec"
                        and isinstance(plan.get("id"), str) and isinstance(plan.get("text"), str) else None)
        if pending_plan is not None and isinstance(plan.get("warning"), str) and plan["warning"]:
            pending_plan["warning"] = plan["warning"]
        active = datetime.fromtimestamp(controller.active_at, timezone.utc)
        self._feed_usage(conv)
        # What the running turn has cost so far but the engine has not booked yet (Devin gives a prompt's figure only when
        # it ends): shown with the total, so the number moves while the turn runs, but never fed to the ledger.
        total = conv.ledger.total
        pending_of = getattr(controller, "usage_pending", None)
        pending = pending_of() if pending_of is not None else None
        if pending is not None:
            total = pending if total is None else total.plus(pending)
        usage_models = dict(conv.ledger.by_model)
        pending_models = getattr(controller, "usage_pending_models", None)
        if pending_models is not None:
            for native, tokens in pending_models().items():
                key = self.catalog.page_id(conv.engine, native)
                usage_models[key] = usage_models.get(key, 0) + tokens.total
        # Until the live session reports its own, the page keeps what was known when it was last cached.
        usage = usage_json(total) or cached.get("usage")
        if total is None and not usage_models:
            usage_models = cached.get("usageModels") or {}
        found = speed_of(controller.transcript.turns)
        speed = found.to_json() if found is not None else cached.get("speed")
        self._learn_window(conv, model_id)
        context = context_json(controller.context, window_hint=(model or {}).get("window") or self._windows.get(model_id))
        if context.get("used") is None and not context.get("compacted") and cached.get("context") and self._cache_fits(context, cached["context"]):
            context = cached["context"]
        return {
            "id": conv.cid,
            "projectId": conv.project,
            "title": self._title_of(conv),
            "mark": self._mark(conv),
            "workingLabel": ("正在生成会话名称…" if conv.naming else "正在压缩并切换模型…") if conv.switch_task is not None else ("本地缓存 · " if conv.preview else "") + controller.working_label,
            "modified": format_relative(active),
            "modifiedAt": int(controller.active_at * 1000),
            "engine": conv.engine,
            "nativeId": controller.session_id or "",
            "pinned": conv.sidebar_key in self.settings.pinned_sessions,
            "settings": settings,
            "usage": usage,
            "usageModels": usage_models,
            "speed": speed,
            "context": context,
            "queued": self._queued_json(conv),
            "preparing": ({"id": conv.preparing.id, "text": conv.preparing.text,
                           "attachments": [self._attachment_view(a) for a in conv.preparing.attachments]}
                          if conv.preparing is not None else None),
            "agents": self._agents_json(conv),
            "fusion": self._shown_fusion(conv),
            "fusionPolicy": ({"workers": list(controller._fusion.registry.policy.workers), "light": controller._fusion.registry.policy.light,
                              "maxCalls": controller._fusion.registry.policy.max_calls, "calls": controller._fusion.registry.calls,
                              "maxTokens": controller._fusion.registry.policy.max_tokens, "knownTokens": controller._fusion.known_tokens(),
                              "unknown": controller._fusion.registry.unmetered, "hadUnknown": controller._fusion.registry.had_unknown}
                             if getattr(controller, "_fusion", None) is not None else None),
            "pendingPlan": pending_plan,
            "goal": conv.goal.text if conv.goal is not None else "",
            "handoff": (
                {
                    "carried": handoff.carried,
                    "truncated": handoff.truncated,
                    "attachments": handoff.files + len(handoff.attachments),
                    "to": (model or {}).get("label", ""),
                }
                if handoff is not None and not handoff.empty
                else None
            ),
        }

    def _flush(self, conv: Conversation) -> None:
        conv.dirty = False
        controller = conv.controller
        transcript = controller.transcript
        turns = transcript.turns
        # Keep the local history through connection failures as well as loading. An error turn is not loaded history.
        hold = conv.preview and (controller.state is SessionState.OPENING or not getattr(controller, "_history_loaded", True))
        if hold and not conv.sent_ids:  # a reloaded page needs the local history again, even while still offline
            self.emit("turns", {"id": conv.cid, "turns": conv.cached_turns, "all": True})
            conv.sent_ids = ["preview"]
        if conv.preview and not hold:
            conv.preview = False
            conv.cached_turns = []
        ids = [turn.id for turn in turns]
        replace = ids[: len(conv.sent_ids)] != conv.sent_ids
        if replace:
            conv.sent_versions = {}
        changed = [t for t in turns if conv.sent_versions.get(t.id) != t.version]
        if (changed or replace) and not hold and conv.switch_task is None:
            built = []
            for turn in changed:
                previous = conv.json_cache.get(turn.id) if not replace else None
                if previous is not None and previous[0] != conv.sent_versions.get(turn.id):
                    previous = None  # A cache write may have rebuilt this turn before the page received it.
                entry = (turn.version, turn_json(turn, self._thumb))
                conv.json_cache[turn.id] = entry
                built.append(changed_blocks(entry[1], previous[1] if previous is not None else None))
            payload = {"id": conv.cid, "turns": built, "all": replace}
            conv.sent_ids = ids
            for turn in changed:
                conv.sent_versions[turn.id] = turn.version
            if replace:
                conv.json_cache = {i: conv.json_cache[i] for i in ids if i in conv.json_cache}
            self.emit("turns", payload)
            self._mark_cache_dirty(conv)
        meta = self._session_meta(conv)
        self._note_run(conv)
        patch = {key: value for key, value in meta.items() if conv.meta.get(key, _MISSING) != value}
        if patch:
            conv.meta.update(patch)
            self.emit("session", {"id": conv.cid, "patch": {"id": conv.cid, **patch}})
            if "usage" in patch or "context" in patch or "speed" in patch:
                conv.cache_dirty = self.cache.enabled
        self._notify_session(conv, meta["mark"])
        if conv.cache_dirty:
            self._mark_cache_dirty(conv)  # (it only schedules the write once the session is idle)
        self._flush_agent(conv)
        self._settle_inflight(conv)

    def _settle_inflight(self, conv: Conversation) -> None:
        """A message with attachments whose turn failed goes back into the composer: retrying is one Enter away."""
        pending = conv.inflight
        if pending is None:
            return
        turn, text, attachments = pending
        if turn.running:
            return
        conv.inflight = None
        submitted = getattr(conv.controller, "prompt_submitted", None)
        if submitted is not None and submitted(turn):
            return  # an error stopping an accepted native turn is not a failed delivery
        if getattr(conv.controller, "_unsent_prompt", None) == turn.id:
            conv.unsent_turns.add(turn.id)
        if turn.id not in conv.unsent_turns and (not attachments or turn.status is not TurnStatus.FAILED):
            return
        if not attachments:
            self._restore(conv, [(text, ())], "这条消息没有发出，已放回输入框，可以重试。")
            return
        # All of them go back, whatever was put into the composer meanwhile: the notice promises a retry that is one
        # Enter away, so no attachment may stay behind. When the lot is more than a message usually holds the user is
        # told by how much; the send asks them (``_m_session_send``) rather than refusing or dropping one.
        conv.drafts = [*attachments, *conv.drafts]
        over = overage([*self._carried_attachments(conv), *conv.drafts])
        self.emit("restore", {"id": conv.cid, "text": text, "attachments": [self._attachment_view(a) for a in conv.drafts]})
        self.say(RETRY_NOTICE if not over.over else f"{RETRY_OVER_NOTICE}加上输入框里原有的附件，{over.describe()}，重试时会让你确认。")

    def _attachment_view(self, attachment: Attachment) -> dict[str, Any]:
        return attachment_json(attachment, self._thumb)

    # ------------------------------------------------------------------ projects

    async def _m_project_add(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = params.get("path")
        if not folder:
            start = self.settings.projects[0] if self.settings.projects else str(Path.home())
            folder = await self.platform.pick_folder(start)
            if not folder:
                return {"added": False}
        if not Path(folder).is_dir():
            raise AppError(f"不是有效的文件夹：{folder}")
        if not self.settings.add_project(folder):
            raise AppError("这个文件夹已经在项目列表里了。")
        self._save()
        added = self.settings.projects[0]
        self._spawn(self._reload_project(added), quiet=True)
        self._emit_sidebar()
        return {"added": True, "id": added}

    async def _m_project_remove(self, params: dict[str, Any]) -> None:
        folder = params["id"]
        for conv in [c for c in self.order if same_folder(c.project, folder)]:
            await self._close_conversation(conv)
        self.settings.remove_project(folder)
        self._saved.pop(normalize_folder(folder), None)
        self._save()
        self._emit_sidebar()

    async def _m_project_refresh(self, params: dict[str, Any]) -> None:
        folders = [params["id"]] if params.get("id") else list(self.settings.projects)
        await asyncio.gather(*(self._reload_project(f) for f in folders), return_exceptions=True)

    async def _m_project_reorder(self, params: dict[str, Any]) -> dict[str, Any]:
        ident, target, after = params.get("id"), params.get("targetId"), params.get("after", False)
        if not isinstance(ident, str) or not isinstance(target, str) or not isinstance(after, bool):
            raise AppError("项目顺序不合法。")
        try:
            source_key, target_key = folder_key(ident), folder_key(target)
        except (OSError, RuntimeError, ValueError):
            raise AppError("项目顺序不合法。") from None
        keys: dict[str, str] = {}
        for item in self.settings.projects:
            try:
                keys.setdefault(folder_key(item), item)
            except (OSError, RuntimeError, ValueError):
                continue
        source, mark = keys.get(source_key), keys.get(target_key)
        if source is None or mark is None:
            raise AppError("只能拖动列表中的项目。")
        if source != mark:
            move_relative(self.settings.projects, source, mark, after)
            self._save()
            self._emit_sidebar()
        return {"ok": True}

    async def _m_project_reveal(self, params: dict[str, Any]) -> None:
        self.platform.reveal(params["id"])

    # ------------------------------------------------------------------ opening and closing sessions

    def _total_open(self) -> int:
        return len(self.order)

    def _has_api_models(self) -> bool:
        """Whether the model list holds a model of an API provider: it runs in the same engine as the ChatGPT plan's models, so
        that engine's saved conversations are listed for it even when the plan is switched off."""
        return self.catalog is not None and any(model.get("engine") == "chatgpt" for model in self.catalog.models)

    async def _make_room(self, *, keep: Conversation | None = None) -> None:
        """Close the idle conversation that was used longest ago until there is a free slot, or say there is none."""
        limit = self.settings.max_open_sessions
        while self._total_open() >= limit:
            idle = sorted(
                (c for c in self.order if c is not keep and c.controller.state is SessionState.READY),
                key=lambda c: c.controller.used_at,
            )
            if not idle:
                raise SessionLimitError(f"最多同时打开 {limit} 个会话；请先停止或关闭一个。")
            victim = idle[0]
            reopenable = any(turn.user_block is not None for turn in victim.controller.transcript.turns)
            victim.controller.eviction_notice = eviction_message(victim.controller.title, reopenable=reopenable)
            self.say(victim.controller.eviction_notice)
            await self._close_conversation(victim)

    def _project_for(self, params: Mapping[str, Any]) -> str:
        folder = params.get("projectId")
        if not folder and params.get("fromId") in self._by_cid:
            folder = self._by_cid[params["fromId"]].project
        if not folder and self.settings.projects:
            folder = self.settings.projects[0]
        if not folder:
            raise AppError("请先添加一个项目文件夹。")
        return normalize_folder(folder)

    def _target(self, page_model: str) -> Target:
        """Which engine opens a page model, and under which id (an API provider's model is run by the own loop)."""
        return self.catalog.target(page_model)

    def _open_settings(self, page_model: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
        start = dict(base) if base is not None else self.defaults()
        engine = self._target(page_model).engine
        # The defaults carry the level of the default model's engine; the scales share names (``code``, ``full``), so a
        # session of another engine starts from that engine's own level, not from a same-named one.
        other = base is None and self._target(start.get("model", "")).engine != engine
        if other or start.get("autonomy") not in self._levels_of(engine):
            start["autonomy"] = self._autonomy_of(engine)
        settings, _ = settings_for_model(self.catalog, start, page_model)
        return settings

    async def _open_controller(
        self,
        folder: str,
        page_model: str,
        settings: Mapping[str, str],
        *,
        transcript: Any = None,
        handoff: Any = None,
        replacing: bool = False,
    ) -> Any:
        """``replacing``: the session takes the place of another one in the same tab (an engine change): the limit on
        open sessions does not apply to it (the tab was counted already), so it closes no other conversation."""
        target = self._target(page_model)
        if not target.model:
            raise AppError("还没有可用的模型：先在「设置 → 账号与模型」里登录 ChatGPT、添加一个 API Provider，或登录 Devin。")
        if target.engine == "factory":
            if self.factory is None:
                raise AppError("这个版本未提供 Factory CLI 兼容。")
            return await self.factory.open_new(folder, model=target.model, reasoning=settings.get("reasoning", ""), autonomy=settings.get("autonomy", self.settings.factory_autonomy), mode=settings.get("mode", "auto"), transcript=transcript or Transcript(), handoff=handoff)
        if target.engine == "devin":
            if self.devin is None:
                raise AppError("没有找到 Devin 命令行：先在「设置 → 账号与模型」里安装并登录。")
            return await self.devin.open_new(
                folder,
                model=target.model,
                reasoning=settings.get("reasoning", ""),
                autonomy=settings.get("autonomy", self.settings.devin_autonomy),
                transcript=transcript,
                handoff=handoff,
            )
        if self.chatgpt is None:
            raise AppError("WittyCode 自己的代理循环在这个版本里不可用。")
        try:
            return await self.chatgpt.open_new(
                folder,
                model=target.model,
                reasoning=settings.get("reasoning", ""),
                autonomy=settings.get("autonomy", self.settings.chatgpt_autonomy),
                mode=settings.get("mode", self.settings.mode),
                transcript=transcript,
                handoff=handoff,
            )
        except ChatGPTError as error:
            raise AppError(chatgpt_error_text(error)) from error

    async def _m_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = self._project_for(params)
        if self.settings.last_project != folder:
            self.settings.last_project = folder
            self._save()
        overrides = params.get("settings")
        if overrides is not None and (not isinstance(overrides, dict) or set(overrides) - {"model", "reasoning", "autonomy", "mode"} or any(not isinstance(v, str) for v in overrides.values())):
            raise AppError("新会话的临时设置不合法。")
        base = {**self.defaults(), **overrides} if overrides is not None else None
        fusion = params.get("fusion", False)
        if not isinstance(fusion, bool):
            raise AppError("Fusion 模式设置不合法。")
        policy = None
        page_model = params.get("modelId") or (base or self.defaults())["model"]
        if fusion:
            from desktop.core.fusion.registry import FusionError, Policy
            try:
                policy = Policy(self.settings.fusion_main, tuple(self.settings.fusion_workers), self.settings.fusion_light,
                                0, 0, self.settings.fusion_max_seconds, self.settings.fusion_preference,
                                self.settings.fusion_reasoning, self.settings.fusion_max_seconds_adaptive)
            except FusionError as error:
                raise AppError("请先在设置中配置 Fusion 的主模型、工作模型和轻量模型。") from error
            page_model = policy.main
            target = self.catalog.target(page_model)
            if self.chatgpt is None or self.chatgpt.model_info(page_model if page_model.startswith(GUARDED) else target.model) is None:
                raise AppError("Fusion 主模型当前不可用；不会自动替换成其他模型。")
        settings = self._open_settings(page_model, base)
        if policy is not None:
            from desktop.core.fusion.reasoning import configured_effort
            chosen = self.catalog.find(page_model) or {}
            try:
                effort = configured_effort(policy.reasoning, page_model, chosen.get("efforts", ()))
            except ValueError as error:
                raise AppError("Fusion 主模型的思考等级已不受支持，请在设置中重新选择。") from error
            if effort is not None:
                settings["reasoning"] = effort
        if policy is not None and settings.get("autonomy") in ("plan", "smart"):
            if settings["autonomy"] == "plan":
                settings["mode"] = "spec"
                settings["autonomy"] = "ask"
            else:
                settings["autonomy"] = "code"  # Never inherit native Smart auto-approval into the common tool policy.
        if settings.get("mode") not in ("auto", "spec"):
            raise AppError("新会话的模式不合法。")
        await self._make_room()
        self._select_next = True
        self._opening_meta = settings
        try:
            if policy is not None and page_model.startswith(GUARDED):
                controller = await self.chatgpt.open_new(folder, model=page_model, reasoning=settings.get("reasoning", ""),
                                                         autonomy=settings.get("autonomy", "code"), mode=settings.get("mode", "auto"))
            else:
                controller = await self._open_controller(folder, page_model, settings)
            if policy is not None:
                try:
                    controller.enable_fusion(policy)
                except Exception:
                    await controller.close()
                    raise
        finally:
            self._opening_meta = None
            self._select_next = False
        conv = self._conv_of(controller)
        if controller.state is SessionState.CLOSED:
            raise AppError(controller.last_error or "会话没有启动。")
        return {"id": conv.cid if conv else None}

    async def _m_session_openSaved(self, params: dict[str, Any]) -> dict[str, Any]:
        row_id: str = self._resolve_row(params["id"])
        folder = normalize_folder(params["projectId"])
        for conv in self.order:
            native = getattr(conv.controller, "session_id", None)
            if row_id in (conv.cid, native, *(prefix + (native or "") for prefix in ROW_PREFIXES.values())) or row_id in conv.hidden:
                return {"id": conv.cid}
        # Opening is not a message: the session keeps the time of its last one, so it stays where it was in the list.
        saved = next((item for item in self._saved.get(folder, []) if item.id == row_id), None)
        modified = saved.modified.timestamp() if saved is not None else None
        seed, preview = await self._cached_conversation(row_id, modified)
        history = None
        if row_id in self.settings.session_links.values():
            try:
                history = await asyncio.to_thread(self.history.load, row_id)
                if not history or not same_folder(history["project"], folder):
                    raise ValueError("missing history")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise AppError("这个会话的跨引擎交接记录无法读取，暂不打开，避免丢失之前的对话。") from error
        await self._make_room()
        self._select_next = True
        self._opening_meta = {}
        self._preview = preview
        self._ledger_seed = seed
        self._opening_history = history
        self._opening_saved_row = row_id
        try:
            if row_id.startswith("fy:"):
                if self.factory is None:
                    raise AppError("没有找到 Factory CLI。")
                controller = await self.factory.open_saved(folder, row_id[3:], title=params.get("title") or None, modified=modified)
            elif row_id.startswith(DEVIN_ROW_PREFIX):
                if self.devin is None:
                    raise AppError("没有找到 Devin 命令行。")
                controller = await self.devin.open_saved(
                    folder, row_id[len(DEVIN_ROW_PREFIX) :], title=params.get("title") or None, modified=modified
                )
            elif row_id.startswith(CHATGPT_ROW_PREFIX):
                if self.chatgpt is None:
                    raise AppError("ChatGPT Plan 在这个版本里不可用。")
                try:
                    controller = await self.chatgpt.open_saved(
                        folder, row_id[len(CHATGPT_ROW_PREFIX) :], title=params.get("title") or None, modified=modified
                    )
                except ChatGPTError as error:
                    raise AppError(chatgpt_error_text(error)) from error
            else:
                raise AppError("这个会话属于一个 WittyCode 已不再支持的引擎，打不开了。")
        finally:
            self._opening_meta = None
            self._select_next = False
            self._preview = None
            self._ledger_seed = None
            self._opening_history = None
            self._opening_saved_row = None
        conv = self._conv_of(controller)
        if conv is not None:
            pending = await asyncio.to_thread(self.history.load, "pending-switch:" + row_id)
            if pending is not None and same_folder(pending.get("project", ""), folder):
                target = pending.get("target")
                if isinstance(target, dict) and isinstance(target.get("model"), str) and isinstance(target.get("settings"), dict):
                    conv.pending_model = target
                    conv.dirty = True
                    self._schedule_flush()
        return {"id": conv.cid if conv else None}

    async def _cached_conversation(
        self, row_id: str, modified: float | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The cached copy of a saved session: ``(the copy, the copy if it is also fit to be shown)``.

        Even an older copy is useful while offline; the status label says it is local, and loaded history replaces it.
        Its token ledger is used whatever its age: only the part after the ledger's last figure is added."""
        if not self.cache.enabled:
            return None, None
        snapshot = await asyncio.to_thread(self.cache.load_session, row_id)
        if not snapshot:
            return None, None
        if not snapshot.get("turns"):
            return snapshot, None
        self._learn_window_from(snapshot)
        return snapshot, snapshot

    @staticmethod
    def _ledger_of(snapshot: Mapping[str, Any]) -> UsageLedger:
        """The token ledger a cached session was left with (a copy from before ledgers: its total was the one session's)."""
        if isinstance(snapshot.get("ledger"), Mapping):
            return UsageLedger.from_json(snapshot["ledger"])
        meta = snapshot.get("meta")
        counted = Tokens.of(meta.get("usage")) if isinstance(meta, Mapping) else None
        return UsageLedger(counted, {} if counted is None else {"self": counted})

    def _conversation(self, cid: str) -> Conversation:
        conv = self._by_cid.get(cid)
        if conv is None:
            raise AppError("这个会话已经关闭。")
        return conv

    async def _close_conversation(self, conv: Conversation) -> None:
        if conv.switch_task is not None:
            conv.switch_task.cancel()
            await asyncio.gather(conv.switch_task, return_exceptions=True)
        controller = conv.controller
        if conv.queue:  # closed by the user: they are not lost without a word
            self.say(f"已关闭会话，{len(conv.queue)} 条排队的消息没有发出。")
            conv.queue = []
        self._retired.add(controller)
        self._remove(conv)
        self._schedule_sidebar(conv.project, reload=True)
        self._spawn(controller.close(), quiet=True)

    async def _m_session_close(self, params: dict[str, Any]) -> None:
        await self._close_conversation(self._conversation(params["id"]))

    def _row_for(self, ident: Any) -> tuple[str, Conversation | None]:
        """``(sidebar row id, open conversation)`` of what the page names: an open conversation's id, or a saved row's."""
        if not isinstance(ident, str) or not ident:
            raise AppError("找不到这个会话。")
        conv = self._by_cid.get(ident)
        if conv is not None:
            return self._row_of(conv), conv
        ident = self._resolve_row(ident)
        conv = next((c for c in self.order if self._row_of(c) == ident), None)
        if conv is not None:
            return ident, conv
        known = any(item.id == ident for rows in self._saved.values() for item in rows)
        if not known and ident not in self.settings.archived_sessions:
            raise AppError("找不到这个会话。")
        return ident, None

    def _has_content(self, conv: Conversation, row: str) -> bool:
        """Something was said in it, or it is one of the engine's saved sessions: there is something to archive or name."""
        if not row:
            return False
        return conv.in_history

    async def _m_session_archive(self, params: dict[str, Any]) -> dict[str, Any]:
        row, conv = self._row_for(params.get("id"))
        if conv is not None:
            if conv.controller.busy:
                raise AppError("这个会话还在运行：先停止它，再归档。")
            if not self._has_content(conv, row):
                raise AppError("这个会话还没有内容，直接关闭就行。")
            await self._close_conversation(conv)
            row = conv.sidebar_key or row  # closing may cancel a tentative engine replacement
        self.settings.archived_sessions[row] = int(time.time() * 1000)
        self._save()
        self._emit_sidebar()
        return {"ok": True}

    async def _m_session_unarchive(self, params: dict[str, Any]) -> dict[str, Any]:
        row, _ = self._row_for(params.get("id"))
        if self.settings.archived_sessions.pop(row, None) is None:
            return {"ok": False}
        self._save()
        self._emit_sidebar()
        return {"ok": True}

    async def _m_session_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        row, conv = self._row_for(params.get("id"))
        title = params.get("title")
        if not isinstance(title, str):
            raise AppError("名字不合法。")
        name = " ".join(title.split())[:MAX_SESSION_TITLE]
        if conv is not None and not self._has_content(conv, row):
            raise AppError("这个会话还没有内容，发出第一条消息之后才能改名。")
        if name:
            self.settings.session_titles[row] = name
        else:  # (an empty name gives the engine's own title back)
            self.settings.session_titles.pop(row, None)
        self._save()
        if conv is not None:
            conv.dirty = True
            self._schedule_flush()
        self._emit_sidebar()
        return {"ok": True, "title": name}

    async def _m_session_stop(self, params: dict[str, Any]) -> None:
        conv = self._conversation(params["id"])
        if conv.switch_task is not None:
            conv.switch_task.cancel()
        elif conv.controller.busy:
            self._spawn(conv.controller.interrupt())

    async def _m_session_reconnect(self, params: dict[str, Any]) -> None:
        conv = self._conversation(params["id"])
        if conv.controller.state is SessionState.DEAD:
            self._spawn(conv.controller.reconnect())

    # ------------------------------------------------------------------ commands typed with a slash

    async def _m_session_compact(self, params: dict[str, Any]) -> dict[str, Any]:
        """``/compact``: the conversation so far becomes a summary now, by the engine's own means (the own loop writes it);
        the controller runs it as a turn the page shows."""
        conv = self._conversation(params["id"])
        start = getattr(conv.controller, "start_compaction", None)
        if start is None:
            return {"ok": False, "text": f"{engine_label(conv.engine, None)} 没有提供手动压缩：它自己管上下文。"}
        if not any(turn.user_block is not None and turn.status is TurnStatus.DONE for turn in conv.controller.transcript.turns):
            return {"ok": False, "text": "还没有对话可以压缩。"}
        if conv.queue:
            return {"ok": False, "text": "还有排队的消息没有发出：它们发完后再压缩。"}
        if conv.goal is not None:
            return {"ok": False, "text": "目标还在进行：先用「/goal off」取消它，或等它结束，再压缩。"}
        problem = start()
        if problem:
            return {"ok": False, "text": problem}
        conv.dirty = True
        self._schedule_flush()
        return {"ok": True, "text": ""}

    async def _m_session_goal(self, params: dict[str, Any]) -> dict[str, Any]:
        """``/goal <text>``: work toward it turn after turn, on any engine (``/goal off`` cancels the one in force)."""
        conv = self._conversation(params["id"])
        text = str(params.get("text", "")).strip()
        if text.lower() in goals.STOP_WORDS:
            checkpoint = getattr(getattr(conv.controller, "saved", None), "goal", {})
            if conv.goal is None and not checkpoint:
                return {"ok": True, "text": "现在没有进行中的目标。"}
            conv.goal = None
            self._checkpoint_goal(conv, None)
            conv.dirty = True  # (the page shows no goal any more: closing the window need not warn about it)
            self._schedule_flush()
            return {"ok": True, "text": "目标已取消。"}
        controller = conv.controller
        if not text:
            return {"ok": False, "text": "用法：/goal 目标，例如 /goal 让所有测试通过。"}
        if controller.state is not SessionState.READY or conv.switch_task is not None:
            return {"ok": False, "text": "会话还没就绪，或这一轮还在运行：结束后再设目标。"}
        if conv.queue:
            return {"ok": False, "text": "还有排队的消息没有发出：它们发完后再设目标。"}
        if self._page_settings(conv).get("mode") == "spec":
            return {"ok": False, "text": "Plan 模式只出方案、不动手，做不了目标：先用 /plan 回到 Auto。"}
        resuming = text.lower() == "resume"
        goal = goals.Goal.from_json(getattr(getattr(controller, "saved", None), "goal", {})) if resuming else goals.Goal(text)
        if goal is None:
            return {"ok": False, "text": "没有已保存的目标可以恢复：用 /goal 目标 设置一个新目标。"}
        if resuming and goal.rounds >= goals.ROUNDS:
            return {"ok": False, "text": "已保存目标已用完轮数：检查结果后，用 /goal 目标 明确开始新一批。"}
        await self._refresh_fusion_settings(conv)
        controller = conv.controller
        if self._needs_first_title(conv):
            conv.switch_task = asyncio.current_task()
            try:
                await self._prepare_first_title(conv, goal.text)
            except AppError as error:
                return {"ok": False, "text": str(error)}
            except asyncio.CancelledError:
                if self._shutting_down:
                    raise
                return {"ok": False, "text": "命名已停止，目标没有启动。"}
            finally:
                conv.switch_task = None
                conv.dirty = True
                self._schedule_flush()
        previous = dict(getattr(getattr(controller, "saved", None), "goal", {}))
        if not self._checkpoint_goal(conv, goal):
            controller.saved.goal = previous
            return {"ok": False, "text": "目标未启动：会话保存失败，请先检查存储空间和权限。"}
        conv.goal = goal
        try:
            accepted = controller.send_prompt(goal.next_message() if resuming else goals.opening(text))
        except BaseException:
            conv.goal = None
            self._checkpoint_goal(conv, goals.Goal.from_json(previous))
            raise
        if not accepted:
            conv.goal = None
            self._checkpoint_goal(conv, goals.Goal.from_json(previous))
            return {"ok": False, "text": "现在不能发送：会话没有就绪。"}
        if not resuming or getattr(controller, "_fusion", None) is None:
            self._start_title(conv, goal.text)
        controller.transcript.add_notice(
            f"目标已{'恢复' if resuming else '设定'}：{goal.text}\n模型会一轮轮做下去，每轮都把目标交给它对照结果判断，最多 {goals.ROUNDS} 轮；完成或需要你时会停下，点「停止」随时结束。"
        )
        conv.dirty = True
        self._schedule_flush()
        return {"ok": True, "text": ""}

    def _checkpoint_goal(self, conv: Conversation, goal: goals.Goal | None) -> bool:
        saved = getattr(conv.controller, "saved", None)
        if conv.engine == OWN_ENGINE and saved is not None:
            saved.goal = goal.to_json() if goal is not None else {}
            return not conv.in_history or conv.controller._save_now()
        return True

    def _stop_goal(self, conv: Conversation, notice: str, *, finished: bool = False) -> None:
        self._checkpoint_goal(conv, None if finished else conv.goal)
        conv.goal = None
        conv.controller.transcript.add_notice(notice)
        conv.dirty = True
        self._schedule_flush()

    def _advance_goal(self, conv: Conversation) -> None:
        """A turn has just ended and no message of the user waits: look at how it went. The goal is met, blocked or given up
        (a notice says which), or the next turn starts with the goal put to the model again."""
        goal = conv.goal
        if goal is None:
            return
        controller = conv.controller
        turns = controller.transcript.turns
        turn = turns[-1] if turns else None
        if getattr(controller, "stop_hook_blocked", False):
            self._stop_goal(conv, "目标先停下了：Stop Hook 未通过；模型回合已保留，但目标未标记完成。")
            return
        if turn is None or turn.status is not TurnStatus.DONE:
            why = "这一轮出错了。" if turn is not None and turn.status is TurnStatus.FAILED else "这一轮被停止了。"
            self._stop_goal(conv, "目标先停下了：" + why)
            return
        end = getattr(controller, "last_turn_end", None)
        if conv.engine == OWN_ENGINE and (end is None or end.reason != "done" or getattr(controller, "_save_failed", False)):
            goal.rounds += 1
            self._stop_goal(conv, "目标先停下了：这一轮未正常完成或未成功保存，未接受完成标记，也不会自动续跑。检查结果后可用 /goal resume 继续。")
            return
        texts = [block.text for block in turn.blocks if block.kind == "text" and block.text]
        outcome = goals.step(goal, texts[-1] if texts else "", bool(turn.tools))
        if not outcome.prompt:
            self._stop_goal(conv, outcome.notice, finished=goals.mark_of(texts[-1] if texts else "")[0] == "done")
        else:
            def send() -> None:
                if conv.goal is not goal or conv.cid not in self._by_cid:
                    return
                if controller.send_prompt(outcome.prompt):
                    self._checkpoint_goal(conv, goal)
                    conv.dirty = True
                    self._schedule_flush()
                else:
                    self._stop_goal(conv, "目标先停下了：下一轮没能发出（会话不能接收消息）。")
            fusion = getattr(controller, "_fusion", None)
            if fusion is not None and fusion.pending_settings() is not None:
                async def refresh_then_send() -> None:
                    try:
                        await self._refresh_fusion_settings(conv)
                    except (asyncio.CancelledError, Exception):
                        self._stop_goal(conv, "目标先停下了：Fusion 配置未能更新，下一轮没有发出；检查设置后可恢复目标。")
                        return
                    finally:
                        conv.switch_task = None
                    send()
                conv.switch_task = self._spawn(refresh_then_send(), quiet=True)
            else:
                send()

    async def _m_skills_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """The skills and custom commands the message box offers after a slash, for the project of a conversation (``id``) or a
        project (``projectId``); read from the files every time, so a skill made a moment ago is there."""
        project = ""
        if params.get("id"):
            project = self._conversation(params["id"]).project
        elif params.get("projectId"):
            project = str(params["projectId"])
        if not project:
            project = self.settings.last_project or str(Path.home())
        items = await asyncio.to_thread(skills.listing, project)
        return {"items": items}

    # ------------------------------------------------------------------ messages

    def _start_title(self, conv: Conversation, question: str) -> None:
        """Only existing, unnamed Fusion sessions use the optional legacy path."""
        controller = conv.controller
        if not conv.in_history or getattr(controller, "_fusion", None) is None:
            return
        row = self._row_of(conv)

        def unnamed() -> bool:
            return (not self._shutting_down and self._by_cid.get(conv.cid) is conv and conv.controller is controller
                    and row not in self.settings.session_titles and session_title.is_default(self._title_of(conv)))

        if unnamed() and question.strip() and not controller.fusion_title_attempted:
            controller.prepare_fusion_title(question, unnamed)

    def _discard_draft_title(self, conv: Conversation) -> None:
        if not conv.in_history and conv.draft_title:
            if conv.engine == OWN_ENGINE and conv.controller.saved.title == conv.draft_title:
                conv.controller.saved.title = ""
            conv.draft_title = ""
            self._schedule_sidebar(conv.project)  # its row leaves again with the title it only borrowed

    def _needs_first_title(self, conv: Conversation) -> bool:
        # Factory's title is provided by the native conversation, never by another account.
        return not conv.in_history and conv.engine in (OWN_ENGINE, "devin") and not conv.draft_title

    async def _prepare_first_title(self, conv: Conversation, text: str,
                                   *, current: Callable[[], bool] = lambda: True) -> None:
        if not self._needs_first_title(conv):
            return
        from desktop.core.fusion.title import generate, source

        controller = conv.controller
        own = conv.engine == OWN_ENGINE
        model = controller.model
        account = source(controller, model) if own else self.devin.cached_account().get("email")

        def allowed() -> bool:
            return (not self._shutting_down and current() and self._by_cid.get(conv.cid) is conv
                    and conv.controller is controller and controller.state is SessionState.READY and controller.model == model
                    and (source(controller, model) if own else self.devin.cached_account().get("email")) == account)

        question = text if text.strip() else "（仅发送附件）"
        prompt = session_title.FIRST_SYSTEM + "\n\n" + session_title.prompt(question, first=True)
        conv.naming = True
        self._flush(conv)
        try:
            for attempt in range(1, session_title.FIRST_MAX_ATTEMPTS + 1):
                if not allowed():
                    break
                reason = ""
                try:
                    if own:
                        title = await generate(controller, question, model, account, allowed, draft=True) if account is not None else None
                    else:
                        answer = await devin_side.ask(self.devin, conv.project, prompt, timeout=session_title.FIRST_TIMEOUT)
                        title = session_title.clean(answer, first=True)
                except ChatGPTError as error:
                    title = None
                    reason = " " + chatgpt_error_text(error)
                if not allowed():
                    break
                if title:
                    conv.draft_title = title
                    if own:
                        controller.saved.title = title  # Carry it in the first dispatch checkpoint, not in a draft archive.
                    self._flush(conv)  # Publish the title before dispatching the first main prompt.
                    self._emit_sidebar()  # The named draft is a sidebar row at once, not after the reply.
                    return
                if attempt == session_title.FIRST_MAX_ATTEMPTS:
                    raise AppError(f"自动命名尝试 {attempt} 次后仍未成功，首条消息没有发出。{reason}")
                await asyncio.sleep(0.2)
            raise AppError("会话已变化，首条消息没有发出。")
        finally:
            conv.naming = False

    def _remember_auto_title(self, conv: Conversation, row: str, title: str) -> None:
        self.settings.session_auto_titles[row] = title
        self._save()
        if conv.cid in self._by_cid:
            conv.dirty = True
            self._mark_cache_dirty(conv)
            self._schedule_flush()
        self._schedule_sidebar(conv.project, reload=True)

    async def _m_session_send(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        if conv.naming:
            raise AppError("正在生成会话名称，首条消息还没有发出。")
        if conv.switching or conv.switch_task is not None:
            raise AppError("模型正在切换，稍后再发送。")
        ids = params.get("attachmentIds")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            raise AppError("附件列表不合法。")
        confirmed = params.get("confirmOver")
        if confirmed is not None and not isinstance(confirmed, bool):
            raise AppError("确认标记不合法。")
        # no list: every draft; an empty list: none of them (the page says which cards it shows)
        wanted = {a.id for a in conv.drafts} if ids is None else set(ids)
        attachments = [a for a in conv.drafts if a.id in wanted]
        controller = conv.controller
        carried = self._carried_attachments(conv)
        fusion = getattr(controller, "_fusion", None)
        changing = conv.pending_model is not None or (fusion is not None and fusion.pending_settings() is not None)
        # A queued/new turn belongs to the destination model; steering still belongs to the running source.
        if not changing or (controller.state is SessionState.RUNNING and params.get("how") == "steer"):
            problem = controller.attachment_problem(carried + tuple(attachments))
            if problem:
                raise AppError(problem)
        # More than a message usually holds (the picked ones from an engine change count, they go with this message)
        # is the user's call: they are told by how much and asked once, nothing is refused or dropped. Only past the
        # safety ceiling (a slip, not a choice) is it refused.
        held = overage([*carried, *attachments])
        if held.beyond_ceiling:
            raise AppError(too_many(held))
        if held.over and not confirmed:
            return {
                "ok": False,
                "over": {"count": held.count, "total": held.total, "files": held.files, "bytes": held.bytes, "text": held.describe()},
            }
        draft = str(params.get("text", ""))
        text = draft.strip()
        how = params.get("how")
        if how is not None and how not in FOLLOW_UPS:
            raise AppError("发送方式不合法。")
        state = controller.state
        plan = getattr(getattr(controller, "saved", None), "plan", {})
        if isinstance(plan, dict) and plan.get("status") == "pending" and getattr(controller, "mode", "") != "spec":
            raise AppError("计划仍待确认；请切回 Plan 并明确执行或修改，切换模式和普通回复不会批准计划。")

        def taken() -> None:
            conv.drafts = [a for a in conv.drafts if a.id not in wanted]

        if state is SessionState.RUNNING and how == "steer" and (text or attachments):
            # Said to the turn that is running, now: the engine takes it in at the next step of that turn.
            steer = getattr(controller, "steer", None)
            outcome = await steer(text, tuple(attachments)) if steer is not None else None
            if outcome is not None and outcome.ok:
                taken()
                conv.dirty = True
                self._schedule_flush()
                return {"ok": True, "steered": True}
            if outcome is not None and outcome.over:
                state = controller.state  # the turn ended while it was on its way: an ordinary message now
            else:
                self.say(f"没能引导本轮：{outcome.message if outcome else '这个会话不支持'}。已改为排队，本轮结束后发出。")
        if state is SessionState.OPENING or state is SessionState.RUNNING or (conv.queue and state is SessionState.READY):
            # Typed while the session connects, or a turn runs (or ahead of messages that are still waiting): kept,
            # shown, and sent by itself in its turn, one message at a time.
            if not text and not attachments:
                raise AppError("现在不能发送：没有内容。")
            conv.queue.append(QueuedMessage(f"q{next(self._queue_ids)}", text, tuple(attachments), raw_text=draft))
            taken()
            conv.dirty = True
            self._schedule_flush()
            if state is SessionState.OPENING:
                if conv.queue_task is None or conv.queue_task.done():
                    conv.queue_task = self._spawn(self._send_when_ready(conv), quiet=True)
            elif state is SessionState.READY:
                self._pump(conv)
            return {"ok": True, "queued": True}
        if not text and not attachments:
            raise AppError("现在不能发送：没有内容。")
        if state is not SessionState.READY or not (changing or self._needs_first_title(conv)):
            await self._send_ready(conv, text, attachments, wanted)
            return {"ok": True}

        # Publish the submitted message during handoff or naming, without adding a real turn. Keep the attachments
        # reserved in drafts until the destination accepts them (also used when checking the cross-engine carry limits).
        conv.preparing = QueuedMessage(f"q{next(self._queue_ids)}", draft, tuple(attachments))
        conv.switch_task = asyncio.current_task()
        try:
            self._flush(conv)
            await self._send_ready(conv, text, attachments, wanted)
        except (Exception, asyncio.CancelledError) as error:
            self._restore(conv, [(draft, ())])
            if isinstance(error, asyncio.CancelledError) and not self._shutting_down:
                # QWebChannel does not reply to a bare cancellation; the composer would remain locked.
                raise AppError("已停止发送准备；消息已放回输入框，未自动重试。") from None
            raise
        finally:
            conv.preparing = None
            conv.switch_task = None
            conv.dirty = True
            self._schedule_flush()
        return {"ok": True, "prepared": True}

    async def _send_ready(self, conv: Conversation, text: str, attachments: Sequence[Attachment], wanted: set[str]) -> None:
        if conv.pending_model is not None:
            await self._apply_pending_model(conv)
        await self._refresh_fusion_settings(conv)
        controller = conv.controller
        problem = controller.attachment_problem(self._carried_attachments(conv) + tuple(attachments))
        if problem:
            raise AppError(problem)
        if controller.state is not SessionState.READY:
            raise AppError("现在不能发送：会话没有就绪。")
        await self._prepare_first_title(conv, conv.preparing.text if conv.preparing is not None else text)
        pending = conv.pending_model
        if pending is not None:
            self._set_pending_model(conv, None)  # Persist before accepting the message; a disk failure must not invite replay.
        try:
            accepted = controller.send_prompt(text, attachments)
        except BaseException:
            self._discard_draft_title(conv)
            if pending is not None:
                self._set_pending_model(conv, pending)
            raise
        if not accepted:
            self._discard_draft_title(conv)
            if pending is not None:
                self._set_pending_model(conv, pending)
            raise AppError("现在不能发送：会话没有就绪。")
        self._start_title(conv, text)
        conv.drafts = [a for a in conv.drafts if a.id not in wanted]
        if attachments or not conv.in_history or conv.engine == "devin" or getattr(controller, "_fusion", None) is not None:
            conv.inflight = (controller.transcript.turns[-1], text, tuple(attachments))

    async def _m_session_approvePlan(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        plan_id = params.get("planId")
        approve = getattr(conv.controller, "approve_plan", None)
        if conv.switching or conv.queue or conv.drafts or not isinstance(plan_id, str) or approve is None:
            return {"ok": False}
        return {"ok": bool(approve(plan_id))}

    async def _m_fusion_transferExpert(self, params: dict[str, Any]) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        conv = self._conversation(params["id"])
        fusion = getattr(conv.controller, "_fusion", None)
        agent_id, model, role = params.get("agentId"), params.get("model"), params.get("role")
        if (fusion is None or conv.switching or conv.queue or conv.controller.state is not SessionState.READY
                or not all(isinstance(item, str) for item in (agent_id, model, role))):
            raise AppError("会话未就绪，暂时不能转交专家。")
        try:
            replacement = await fusion.transfer(agent_id, model, role)
        except FusionError as error:
            raise AppError("无法转交：请确认原专家存档完整、新模型可用且已在本会话的授权池中。") from error
        return {"ok": True, "agentId": replacement}

    async def _m_fusion_changeMain(self, params: dict[str, Any]) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        conv = self._conversation(params["id"])
        fusion, model = getattr(conv.controller, "_fusion", None), params.get("model")
        if (fusion is None or conv.switching or conv.switch_task is not None or conv.queue
                or conv.controller.state is not SessionState.READY or not isinstance(model, str)):
            raise AppError("会话未就绪，暂时不能更换 Fusion 主模型。")
        chosen = self.catalog.find(model)
        if not chosen or chosen.get("disabled") or self.catalog.target(model).engine not in ("chatgpt", "devin"):
            raise AppError("主模型当前不可用；未自动更换其他模型。")
        settings, changed = settings_for_model(self.catalog, self._page_settings(conv), model)
        reasoning = settings.get("reasoning", "") if "reasoning" in changed else None
        self._check_fusion_switch(conv)
        conv.switching = True
        try:
            if self._page_settings(conv).get("model") != model:
                await self._compact_model_switch(conv, model)
            changed_main = fusion.change_main(model, reasoning)
            if changed_main:
                await conv.controller.release_inactive_acp()
            return {"ok": changed_main}
        except FusionError as error:
            raise AppError("无法更换主模型：请先处理待审批计划，并确认所选模型可用。") from error
        finally:
            conv.switching = False
            self._feed_usage(conv)
            conv.dirty = True
            self._schedule_flush()

    async def _m_fusion_authorizeModel(self, params: dict[str, Any]) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        conv = self._conversation(params["id"])
        fusion, model = getattr(conv.controller, "_fusion", None), params.get("model")
        if fusion is None or conv.switching or conv.queue or conv.controller.state is not SessionState.READY or not isinstance(model, str):
            raise AppError("会话未就绪，暂时不能授权模型。")
        try:
            return {"ok": fusion.authorize_worker(model)}
        except FusionError as error:
            raise AppError("模型当前不可用，或本会话的工作模型池已满。") from error

    async def _m_fusion_updateBudget(self, params: dict[str, Any]) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        conv = self._conversation(params["id"])
        fusion = getattr(conv.controller, "_fusion", None)
        unknown = params.get("acceptUnknown", False)
        if (fusion is None or conv.switching or conv.queue or conv.controller.state is not SessionState.READY
                or not isinstance(unknown, bool)):
            raise AppError("会话未就绪，暂时不能调整 Fusion 预算。")
        if conv.naming or conv.controller.fusion_title_running:
            raise AppError("正在记录会话标题的用量，请稍后再调整预算。")
        try:
            fusion.update_budget(params.get("maxCalls"), params.get("maxTokens"), accept_unknown=unknown)
        except FusionError as error:
            raise AppError("预算不能低于原上限或已知用量；存在未知用量时须明确确认。") from error
        return {"ok": True}

    async def _m_fusion_resumeExpert(self, params: dict[str, Any]) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        conv = self._conversation(params["id"])
        fusion, agent_id = getattr(conv.controller, "_fusion", None), params.get("agentId")
        if (fusion is None or conv.switching or conv.queue or conv.controller.state is not SessionState.READY
                or not isinstance(agent_id, str)):
            return {"ok": False}
        try:
            await fusion._archive(fusion.archive_source(agent_id))
        except FusionError as error:
            raise AppError("专家存档无法读取，未自动创建新专家。") from error
        if conv.controller.state is not SessionState.READY or conv.switching or conv.queue:
            return {"ok": False}
        return {"ok": fusion.acknowledge(agent_id)}

    async def _until_ready(self, conv: Conversation, timeout: float) -> bool:
        """Wait for a connecting session; ``False`` when it did not get ready (it died, closed, or took too long)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            state = conv.controller.state
            if state in (SessionState.READY, SessionState.RUNNING):
                return True
            if state is not SessionState.OPENING or loop.time() > end or conv.cid not in self._by_cid:
                return False
            await asyncio.sleep(0.05)

    def _agents_json(self, conv: Conversation) -> list[dict[str, Any]]:
        """The sub-agents the conversation's thread has started (an engine that has them), oldest first."""
        agents = getattr(conv.controller, "agents", None) or {}
        out = []
        for agent in agents.values():
            model = self.catalog.find(self.catalog.page_id(conv.engine, agent.agent_model)) if agent.agent_model else None
            child = getattr(agent, "controller", None)
            record = getattr(agent, "record", None)
            tokens = child.usage if child is not None and child.is_open else None
            if tokens is None:
                tokens = conv.ledger.seen.get(f"agent:{agent.session_id}")
            out.append(
                {
                    "id": agent.session_id,
                    "name": agent.name,
                    "role": agent.role,
                    "model": (model or {}).get("label") or agent.agent_model,
                    "effort": agent.agent_effort,
                    "task": agent.task,
                    "status": agent.status,
                    "tokens": usage_json(tokens),
                    "fusionState": getattr(record, "status", None),
                    "fusionModel": getattr(record, "model", None),
                    "fusionRole": getattr(record, "role", None),
                    "fusionIdentity": getattr(record, "identity", None) or None,
                    "unstartedTransfer": bool(record and record.transferred_from and not record.started_at),
                    "startedAt": int(agent.started_at_s * 1000),
                    "finishedAt": int(agent.finished_at_s * 1000) if agent.finished_at_s else None,
                }
            )
        return out

    def _flush_agent(self, conv: Conversation) -> None:
        """The run of the sub-agent on show in the side panel, like ``_flush`` does for the conversation."""
        agent_id = conv.watching
        if agent_id is None:
            return
        agent = (getattr(conv.controller, "agents", None) or {}).get(agent_id)
        if agent is None:
            conv.watching = None
            return
        turns = agent.transcript.turns
        ids = [turn.id for turn in turns]
        replace = ids[: len(conv.agent_sent_ids)] != conv.agent_sent_ids
        if replace:
            conv.agent_sent_versions = {}
        changed = [t for t in turns if conv.agent_sent_versions.get(t.id) != t.version]
        if changed or replace:
            conv.agent_sent_ids = ids
            for turn in changed:
                conv.agent_sent_versions[turn.id] = turn.version
            self.emit("agentTurns", {"id": conv.cid, "agentId": agent_id, "turns": [turn_json(t, self._thumb) for t in changed], "all": replace})

    async def _m_agents_watch(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        agent_id = str(params.get("agentId") or "")
        if agent_id not in (getattr(conv.controller, "agents", None) or {}):
            raise AppError("这个子任务已经不在了。")
        fusion = getattr(conv.controller, "_fusion", None) or getattr(conv.controller, "_paused_fusion", None)
        if fusion is not None:
            view = fusion.agents[agent_id]
            if view.controller is None and not (view.record.transferred_from and not view.record.started_at):
                try:
                    await fusion._open(view, created=False)
                except Exception as error:
                    raise AppError("专家上下文无法恢复，原记录仍保留；不会自动创建新的专家。") from error
        conv.watching = agent_id
        conv.agent_sent_ids, conv.agent_sent_versions = ["fresh"], {}  # the first push is the whole run, replacing the panel's
        self._flush_agent(conv)
        return {"ok": True}

    async def _m_agents_unwatch(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        conv.watching = None
        return {"ok": True}

    def _queued_json(self, conv: Conversation) -> list[dict[str, Any]]:
        return [
            {"id": m.id, "text": m.text, "attachments": [self._attachment_view(a) for a in m.attachments]} for m in conv.queue
        ]

    def _restore(self, conv: Conversation, items: Sequence[tuple[str, Sequence[Attachment]]], notice: str = "") -> None:
        """Messages that did not go out are put back into the composer (words in order, attachments in front)."""
        text = "\n\n".join(words for words, _ in items if words)
        conv.drafts = [*(a for _, attached in items for a in attached), *conv.drafts]
        self.emit("restore", {"id": conv.cid, "text": text, "attachments": [self._attachment_view(a) for a in conv.drafts]})
        if notice:
            self.say(notice)
        conv.dirty = True
        self._schedule_flush()

    def _unqueue_all(self, conv: Conversation, notice: str = "") -> None:
        """The queued messages go back into the composer (withdrawn, or the session never got ready / could not go on)."""
        items, conv.queue = [(m.text, m.attachments) for m in conv.queue], []
        if conv.queue_task is not None and conv.queue_task is not asyncio.current_task():
            conv.queue_task.cancel()
        if items:
            self._restore(conv, items, notice)

    def _return_steers(self, conv: Conversation) -> None:
        """What the user said to a running turn that the engine did not take in (the turn was stopped, or ended first)."""
        take = getattr(conv.controller, "take_unadopted", None)
        items = take() if take is not None else []
        if items:
            self._restore(conv, items, "引导没有来得及生效（本轮已经结束或被停止），内容已放回输入框。")

    def _pump(self, conv: Conversation) -> None:
        """Send the next queued message, if the session can take one now."""
        controller = conv.controller
        if conv.switch_task is not None or conv.switching or not conv.queue or controller.state is not SessionState.READY or conv.cid not in self._by_cid:
            return
        plan = getattr(getattr(controller, "saved", None), "plan", {})
        if isinstance(plan, dict) and plan.get("status") == "pending":
            self._unqueue_all(conv, "计划等待确认：排队的消息没有发出，已放回输入框；请明确执行或修改计划。")
            return
        fusion = getattr(controller, "_fusion", None)
        if fusion is not None and fusion.pending_settings() is not None:
            async def refresh_then_pump() -> None:
                try:
                    await self._refresh_fusion_settings(conv)
                except asyncio.CancelledError:
                    self._unqueue_all(conv, "Fusion 配置更新已停止，排队消息已放回输入框。")
                    return
                except Exception as error:
                    self._unqueue_all(conv, f"Fusion 配置更新失败，排队消息已放回输入框：{error_text(error)}")
                    return
                finally:
                    conv.switch_task = None
                self._pump(conv)
            conv.switch_task = self._spawn(refresh_then_pump(), quiet=True)
            return
        head = conv.queue[0]
        problem = controller.attachment_problem(self._carried_attachments(conv) + tuple(head.attachments))
        if problem:
            self._unqueue_all(conv, problem)
            return
        if self._needs_first_title(conv):
            def current() -> bool:
                return bool(conv.queue) and conv.queue[0] is head

            async def name_then_pump() -> None:
                try:
                    await self._prepare_first_title(conv, head.raw_text if head.raw_text is not None else head.text, current=current)
                except asyncio.CancelledError:
                    self._unqueue_all(conv, "命名已停止，消息已放回输入框。")
                    return
                except Exception as error:
                    if current():
                        self._unqueue_all(conv, error_text(error))
                finally:
                    conv.switch_task = None
                    conv.dirty = True
                    self._schedule_flush()
                self._pump(conv)

            conv.switch_task = self._spawn(name_then_pump(), quiet=True)
            return
        try:
            accepted = controller.send_prompt(head.text, head.attachments)
        except Exception as error:
            self._discard_draft_title(conv)
            self._unqueue_all(conv, f"消息没有发出，已放回输入框：{error_text(error)}")
            return
        if not accepted:
            self._discard_draft_title(conv)
            self._unqueue_all(conv, "会话没有就绪，排队消息已放回输入框。")
            return
        self._start_title(conv, head.text)
        conv.queue.pop(0)
        if head.attachments or not conv.in_history or conv.engine == "devin" or fusion is not None:
            conv.inflight = (controller.transcript.turns[-1], head.text, head.attachments)
        conv.dirty = True
        self._schedule_flush()

    def _follow_queue(self, conv: Conversation) -> None:
        """The state of the conversation changed with messages waiting (or a turn that has just ended)."""
        if conv.cid not in self._by_cid or conv.switching or conv.switch_task is not None:
            return
        controller = conv.controller
        state = controller.state
        if state is SessionState.RUNNING:
            conv.ran = True
        elif state is SessionState.READY:
            previous_goal = conv.goal
            turns = controller.transcript.turns
            failed = conv.ran and bool(turns) and turns[-1].status is TurnStatus.FAILED
            ended = conv.ran
            if conv.ran:
                self._curate(conv)
            conv.ran = False
            if conv.queue and failed:
                # A turn that failed would most likely fail the next one the same way; nothing is sent behind its back.
                self._unqueue_all(conv, "上一轮没有成功：排队的消息没有发出，已放回输入框。")
            elif conv.queue:
                self._pump(conv)
            elif ended:
                self._advance_goal(conv)
            self._notify_complete(conv, previous_goal)
        elif state is SessionState.DEAD and conv.queue:
            if conv.ran:
                self._unqueue_all(conv, "连接断开了：排队的消息没有发出，已放回输入框，重新连接后可以再发。")
            else:
                self._unqueue_all(conv, "没有连上：消息已放回输入框，可以重试。")
            conv.ran = False

    async def _send_when_ready(self, conv: Conversation) -> None:
        """Watches a connecting session for the messages typed meanwhile: they go out when it is ready (the queue does
        that), or come back when it never gets ready."""
        controller = conv.controller
        ready = await self._until_ready(conv, QUEUE_SECONDS)
        if not conv.queue or conv.cid not in self._by_cid:
            return
        if ready and conv.controller is controller:
            self._pump(conv)
            return
        self._unqueue_all(conv, "没有连上：消息已放回输入框，可以重试。")

    async def _m_session_unqueue(self, params: dict[str, Any]) -> dict[str, Any]:
        """Take queued messages back into the composer: the one named by ``queueId``, or all of them."""
        conv = self._conversation(params["id"])
        wanted = params.get("queueId")
        if wanted is not None and not isinstance(wanted, str):
            raise AppError("撤回的消息编号不合法。")
        if not conv.queue or (wanted is not None and all(m.id != wanted for m in conv.queue)):
            return {"ok": False}
        if wanted is None:
            self._unqueue_all(conv)
            return {"ok": True}
        taken = next(m for m in conv.queue if m.id == wanted)
        conv.queue = [m for m in conv.queue if m is not taken]
        if not conv.queue and conv.queue_task is not None:
            conv.queue_task.cancel()
        self._restore(conv, [(taken.text, taken.attachments)])
        return {"ok": True}

    async def _m_session_editQueued(self, params: dict[str, Any]) -> dict[str, Any]:
        """Change the words of a waiting message; it keeps its place in the queue and its attachments."""
        conv = self._conversation(params["id"])
        wanted, text = params.get("queueId"), params.get("text")
        if not isinstance(wanted, str) or not isinstance(text, str):
            raise AppError("排队的消息或它的内容不合法。")
        index = next((i for i, m in enumerate(conv.queue) if m.id == wanted), None)
        if index is None:
            return {"ok": False}  # (it went out, or was taken back, while the user was typing)
        item = conv.queue[index]
        trimmed = text.strip()
        if not trimmed and not item.attachments:
            raise AppError("消息不能是空的：不想发这条，请点取消。")
        conv.queue[index] = dataclasses.replace(item, text=trimmed, raw_text=text)
        conv.dirty = True
        self._schedule_flush()
        return {"ok": True}

    async def _m_session_sendQueued(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send a waiting message now: to the turn that is running (it reads it at its next step), or, when no turn is
        running any more, as the message that goes next."""
        conv = self._conversation(params["id"])
        wanted = params.get("queueId")
        if not isinstance(wanted, str):
            raise AppError("排队的消息编号不合法。")
        item = next((m for m in conv.queue if m.id == wanted), None)
        if item is None:
            return {"ok": False}
        controller = conv.controller
        state = controller.state
        if state is SessionState.RUNNING:
            steer = getattr(controller, "steer", None)
            index = conv.queue.index(item)
            conv.queue.remove(item)  # (out of the queue first: the turn may end while the engine takes it in, and it must not go twice)
            outcome = await steer(item.text, item.attachments) if steer is not None else None
            if outcome is not None and outcome.ok:
                conv.dirty = True
                self._schedule_flush()
                return {"ok": True, "steered": True}
            if outcome is not None and outcome.over:
                conv.queue.insert(0, item)  # (the turn ended on the way: it is the next message now)
                self._pump(conv)
                conv.dirty = True
                self._schedule_flush()
                return {"ok": True}
            conv.queue.insert(min(index, len(conv.queue)), item)
            self.say(f"没能引导本轮：{outcome.message if outcome else '这个会话不支持'}。这条还在排队，本轮结束后发出。")
            if controller.state is SessionState.READY:  # (the turn ended meanwhile: nothing else would send it)
                self._pump(conv)
            conv.dirty = True
            self._schedule_flush()
            return {"ok": False}
        if state is SessionState.READY:
            conv.queue.remove(item)
            conv.queue.insert(0, item)
            self._pump(conv)
            return {"ok": True}
        self.say("会话还没连上：连上之后这条会自动发出。")
        return {"ok": False}

    async def _m_session_answerPermission(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        agent_id = params.get("agentId")
        if agent_id:  # a sub-agent's approval, shown in the side panel
            answer = getattr(conv.controller, "answer_agent_permission", None)
            return {"ok": bool(answer and answer(str(agent_id), params["promptId"], params["value"]))}
        return {"ok": conv.controller.answer_permission(params["promptId"], params["value"])}

    async def _m_session_answerQuestion(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        agent_id = params.get("agentId")
        if agent_id:
            answer = getattr(conv.controller, "answer_agent_question", None)
            return {"ok": bool(answer and answer(str(agent_id), params["promptId"], params.get("answers")))}
        return {"ok": conv.controller.answer_question(params["promptId"], params.get("answers"))}

    # ------------------------------------------------------------------ attachments

    def _own_draft(self, attachment: Attachment) -> Attachment:
        return dataclasses.replace(attachment, id=f"d{next(self._draft_ids)}")

    @staticmethod
    def _carried_attachments(conv: Conversation) -> tuple[Attachment, ...]:
        """Pictures and PDF files the user picked at an engine change, still to go out with the next message."""
        handoff = getattr(conv.controller, "handoff", None)
        return tuple(handoff.attachments) if handoff is not None and not handoff.empty else ()

    async def _admit(self, conv: Conversation, loaded: Sequence[Attachment | Rejection]) -> dict[str, Any]:
        candidates = [item for item in loaded if not isinstance(item, Rejection)]
        rejected = [item for item in loaded if isinstance(item, Rejection)]
        # the next message also carries what was picked at the engine change: it counts with the box
        carried = self._carried_attachments(conv)
        accepted, over = admit([*carried, *conv.drafts], candidates)  # only the safety ceiling refuses
        rejected += over
        added = [self._own_draft(item) for item in accepted]
        conv.drafts.extend(added)
        held = overage([*carried, *conv.drafts])
        return {
            "added": [self._attachment_view(a) for a in added],
            "rejected": describe_rejections(rejected) if rejected else "",
            # more than a message usually holds is allowed; the user hears it now and is asked when they send
            "over": f"现在{held.describe()}，发送时会让你确认。" if held.over else "",
        }

    async def _m_attachments_add(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        loaded: list[Attachment | Rejection] = []
        for item in params.get("files", []):
            try:
                data = base64.b64decode(item.get("data", ""), validate=False)
            except ValueError:
                loaded.append(Rejection(str(item.get("name", "?")), "数据已损坏。"))
                continue
            loaded.append(await asyncio.to_thread(self.platform.load_bytes, str(item.get("name") or "粘贴的文件"), data))
        return await self._admit(conv, loaded)

    async def _m_attachments_pick(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
        paths = await self.platform.pick_files(conv.project)
        loaded = [await asyncio.to_thread(self.platform.load_path, path) for path in paths]
        return await self._admit(conv, loaded)

    async def _m_attachments_remove(self, params: dict[str, Any]) -> None:
        conv = self._conversation(params["id"])
        conv.drafts = [a for a in conv.drafts if a.id != params["attachmentId"]]

    async def _m_attachments_full(self, params: dict[str, Any]) -> dict[str, Any]:
        wanted = params["attachmentId"]
        for conv in ([self._by_cid[params["id"]]] if params.get("id") in self._by_cid else []) or self.order:
            found = next((a for a in conv.drafts if a.id == wanted), None) or conv.controller.transcript.attachment(wanted)
            if found is not None:
                return {"url": self.platform.full_picture(found)}
        return {"url": None}

    # ------------------------------------------------------------------ files the conversation names

    async def _file_named(self, params: dict[str, Any]) -> tuple[Path, filetools.Ref]:
        conv = self._conversation(params["id"])
        raw = params.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise AppError("没有指定文件。")
        try:
            return await asyncio.to_thread(filetools.resolve, raw, conv.project)
        except filetools.FileProblem as problem:
            raise AppError(str(problem)) from problem

    async def _m_files_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Where a path the way the model wrote it is, for the page's menu (copy the path)."""
        path, ref = await self._file_named(params)
        return {"path": str(path), "isDir": path.is_dir(), "line": ref.line}

    async def _m_files_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Text or preview metadata; this read does not own the video player."""
        path, ref = await self._file_named(params)
        kind = await asyncio.to_thread(filetools.preview_kind, path)
        if kind is not None:
            try:
                size = await asyncio.to_thread(filetools.preview_size, path, kind)
                pages = [{"w": w, "h": h} for w, h in self.platform.pdf_pages(str(path))] if kind == "pdf" else []
            except filetools.FileProblem as problem:
                raise AppError(str(problem)) from problem
            return {
                "kind": kind,
                "path": str(path),
                "name": path.name,
                "dir": str(path.parent),
                "text": "",
                "size": size,
                "lines": 0,
                "encoding": "",
                "truncated": False,
                "markdown": False,
                "line": None,
                "lineEnd": None,
                "pages": pages,
            }
        try:
            panel = await asyncio.to_thread(filetools.read_for_panel, path)
        except filetools.FileProblem as problem:
            raise AppError(str(problem)) from problem
        text, cut_lines = panel.text, False
        if panel.lines > filetools.PANEL_LINES:
            text, cut_lines = "\n".join(panel.text.split("\n")[: filetools.PANEL_LINES]), True
        return {
            "kind": "text",
            "pages": [],
            "path": str(path),
            "name": path.name,
            "dir": str(path.parent),
            "text": text,
            "size": panel.size,
            "lines": panel.lines,
            "encoding": panel.encoding,
            "truncated": panel.truncated or cut_lines,
            "markdown": panel.markdown,
            "line": ref.line,
            "lineEnd": ref.end,
        }

    async def _m_files_page(self, params: dict[str, Any]) -> dict[str, Any]:
        """A PDF page at the requested width, or the bytes of a picture or Office file (``data:`` URIs)."""
        path, _ = await self._file_named(params)
        kind = await asyncio.to_thread(filetools.preview_kind, path)
        page, width = params.get("page", 0), params.get("width", 900)
        if kind is None or kind == "video":
            raise AppError("这个文件不能在侧栏里画出来。")
        if not (isinstance(page, int) and not isinstance(page, bool) and isinstance(width, int) and not isinstance(width, bool)) or page < 0:
            raise AppError("页码或宽度不合法。")
        try:
            if kind == "pdf":
                url = self.platform.pdf_page(str(path), page, width)
            elif kind == "image":
                if path.suffix.lower() in filetools.CONVERTED_IMAGES:
                    url = await asyncio.to_thread(self.platform.converted_picture, str(path))
                else:
                    url = await asyncio.to_thread(filetools.image_data_url, path)
            elif page == 0:
                url = await asyncio.to_thread(filetools.office_data_url, path, kind)
            else:
                raise AppError("页码不合法。")
        except filetools.FileProblem as problem:
            raise AppError(str(problem)) from problem
        return {"url": url}

    async def _m_files_inlineImage(self, params: dict[str, Any]) -> dict[str, Any]:
        """Automatic reads always use the message's own project and the restricted media reader."""
        cid = params.get("id")
        if not isinstance(cid, str):
            raise AppError("没有指定媒体所属的会话。")
        conv = self._conversation(cid)
        async with self._inline_images:
            if self._shutting_down or self._by_cid.get(cid) is not conv:
                raise AppError("这个会话已经关闭。")
            try:
                image = await asyncio.to_thread(filetools.inline_image, params.get("path"), conv.project)
            except filetools.FileProblem as problem:
                raise AppError(str(problem)) from problem
        if self._shutting_down or self._by_cid.get(cid) is not conv:
            raise AppError("这个会话已经关闭。")
        return image

    async def _m_files_inlineVideoPoster(self, params: dict[str, Any]) -> dict[str, Any]:
        """One short-lived, silent decode at a time, wholly separate from the playback lease."""
        cid, width = params.get("id"), params.get("width")
        if not isinstance(cid, str):
            raise AppError("没有指定媒体所属的会话。")
        if not _is_int(width) or width <= 0:
            raise AppError("视频预览宽度不合法。")
        conv = self._conversation(cid)

        def check_session() -> None:
            if self._shutting_down or self._by_cid.get(cid) is not conv:
                raise AppError("这个会话已经关闭。")

        async with self._inline_video_posters:
            check_session()
            try:
                source = await self._open_inline_video(params.get("path"), conv.project)
                with source.file:
                    check_session()
                    info = await self.platform.video_poster(source, min(width, 1280))
                    check_session()
                    return info
            except filetools.FileProblem as problem:
                raise AppError(str(problem)) from problem

    def _close_video_preview(self, *, replaced: bool = False) -> None:
        lease, self._video_preview = self._video_preview, None
        if lease is not None:
            self.platform.video_close()
            if replaced:
                self.emit("files.video", {"token": lease[1], "released": True})

    async def _open_inline_video(self, raw: Any, project: str) -> filetools.InlineVideoSource:
        opening = asyncio.create_task(asyncio.to_thread(filetools.open_inline_video, raw, project))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            # A thread already opening a file cannot be cancelled; reclaim its result if the bridge goes away.
            def dispose(task: asyncio.Task[filetools.InlineVideoSource]) -> None:
                if not task.cancelled() and task.exception() is None:
                    task.result().file.close()

            opening.add_done_callback(dispose)
            raise

    async def _m_files_video(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Only the owning session and mount token may change or close a live video."""
        cid, token, action = params.get("id"), params.get("token"), params.get("action")
        if not isinstance(cid, str) or not isinstance(token, str) or not 1 <= len(token) <= 64:
            raise AppError("没有指定视频预览。")
        lease = (cid, token)
        if action == "open":
            conv = self._conversation(cid)
            width = params.get("width")
            if not _is_int(width) or width <= 0:
                raise AppError("视频预览宽度不合法。")
            inline = params.get("inline", False)
            if type(inline) is not bool:
                raise AppError("视频预览类型不合法。")
            if self._shutting_down:
                raise AppError("视频预览已关闭。")
            self._close_video_preview(replaced=True)
            self._video_preview = lease

            def publish(payload: dict[str, Any]) -> None:
                if self._video_preview is lease:
                    self.emit("files.video", {**payload, "token": token})

            source: filetools.InlineVideoSource | None = None
            try:
                name: str
                if inline:
                    source = await self._open_inline_video(params.get("path"), conv.project)
                    name = source
                else:
                    path, _ = await self._file_named(params)
                    if await asyncio.to_thread(filetools.preview_kind, path) != "video":
                        raise AppError("这不是可以在侧栏预览的视频文件。")
                    name = str(path)
                if self._video_preview is not lease:
                    raise AppError("视频预览已关闭。")
                info = await self.platform.video_open(name, min(width, 1280), publish)
                if self._video_preview is not lease:
                    raise AppError("视频预览已关闭。")
                return info
            except BaseException as problem:
                if self._video_preview is lease:
                    self._close_video_preview()
                if isinstance(problem, filetools.FileProblem):
                    raise AppError(str(problem)) from problem
                raise
            finally:
                if source is not None:
                    source.file.close()
        if action not in ("play", "pause", "seek", "volume", "close"):
            raise AppError("视频操作不合法。")
        if self._video_preview != lease:
            return None  # An old panel's cleanup or delayed control must not affect its replacement.
        if action == "close":
            self._close_video_preview()
            return None
        if action == "seek" and (not _is_int(params.get("position")) or not 0 <= params["position"] < 2**53):
            raise AppError("视频播放位置不合法。")
        if action == "volume":
            volume = params.get("volume")
            if type(volume) not in (int, float) or not 0 <= volume <= 1 or type(params.get("muted")) is not bool:
                raise AppError("视频音量不合法。")
        self.platform.video_control(action, params)
        return None

    async def _m_files_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open a file outside the window: with the default program, by the system's "open with" choice, or selected in its
        folder. A program or a script is never run by a click: the default way shows it in its folder instead."""
        how = params.get("how")
        if how not in ("default", "choose", "reveal"):
            raise AppError("打开方式不合法。")
        path, _ = await self._file_named(params)
        target = str(path)
        if how == "reveal":
            self.platform.show_in_folder(target)
        elif how == "choose":
            self.platform.open_with(target)
        elif path.is_dir():
            self.platform.open_path(target)
        elif filetools.is_runnable(path):
            self.platform.show_in_folder(target)
            return {"ok": True, "shown": True, "message": f"「{path.name}」是程序或脚本，不会直接运行：已在文件夹中显示。想用别的程序打开，右键选「选择打开方式」。"}
        else:
            self.platform.open_path(target)
        return {"ok": True}

    # ------------------------------------------------------------------ the settings under the message box

    async def _m_session_choose(self, params: dict[str, Any]) -> dict[str, Any]:
        field_name: str = params["field"]
        value: str = params["value"]
        if field_name not in ("model", "reasoning", "autonomy", "mode"):
            raise AppError("没有这个设置。")
        cid = params.get("id")
        if not cid:
            if field_name == "model" and value == "fusion":
                raise AppError("Fusion 是会话模式，不能保存为默认模型。")
            self._set_default(field_name, value)
            return {"ok": True}
        conv = self._conversation(cid)
        if conv.switching or conv.switch_task is not None:
            raise AppError("模型正在压缩交接，稍后再修改设置。")
        if field_name == "model" and value == "fusion" and conv.controller.state is SessionState.OPENING:
            raise AppError("会话就绪后才能切换 Fusion。")
        if conv.controller.state is SessionState.OPENING:
            # Chosen while the session connects: shown at once, applied the moment it is ready.
            conv.requested[field_name] = value
            conv.dirty = True
            self._schedule_flush()
            if not await self._until_ready(conv, QUEUE_SECONDS):
                return {"ok": False}
        if field_name == "model":
            carry_ids = params.get("attachments")
            if carry_ids is not None and not (isinstance(carry_ids, list) and all(isinstance(i, str) for i in carry_ids)):
                raise AppError("这个设置的值不合法。")
            over_ok = params.get("overLimit")
            if over_ok is not None and not isinstance(over_ok, bool):
                raise AppError("这个设置的值不合法。")
            if conv.controller.transcript.turns or conv.pending_model is not None:
                return self._select_model(conv, value, carry_ids, over_ok is True)
            return await self._choose_model(conv, value, carry_ids, over_ok is True)
        if conv.pending_model is not None and field_name == "reasoning":
            model = self.catalog.find(conv.pending_model["settings"]["model"]) or {}
            if conv.pending_model["model"] == "fusion" or value not in model.get("efforts", ()):
                raise AppError("所选模型不支持这个推理档位。")
            pending = {**conv.pending_model, "settings": {**conv.pending_model["settings"], "reasoning": value}}
            self._set_pending_model(conv, pending)
            return {"ok": True}
        ok = await self._update_live(conv, **{field_name: value})
        if ok and conv.pending_model is not None:
            pending = {**conv.pending_model, "settings": {**conv.pending_model["settings"], field_name: self._page_settings(conv).get(field_name, value)}}
            self._set_pending_model(conv, pending)
        return {"ok": ok}

    def _set_default(self, field_name: str, value: str, *, engine: str | None = None) -> None:
        settings = self.settings
        if field_name == "model":
            settings.default_model = value
            # The level and permission that go with the new model's engine.
            fixed, _ = settings_for_model(self.catalog, self._engine_defaults(value), value)
            settings.reasoning_effort = fixed.get("reasoning", "")
            self._prefetch_profiles(value)
        elif field_name == "reasoning":
            settings.reasoning_effort = value
        elif field_name == "autonomy":
            current = engine or self.catalog.engine_of(settings.default_model)
            if current == "factory" and value in ("off", "low", "medium", "high"):
                settings.factory_autonomy = value
            elif current == "devin" and value in DEVIN_PERMISSIONS:
                settings.devin_autonomy = value
            elif current == "chatgpt" and value in CHATGPT_PERMISSIONS:
                settings.chatgpt_autonomy = value
        elif field_name == "mode" and value in ("auto", "spec"):
            settings.mode = value
        self._save()
        self.emit("defaults", {"defaults": self.defaults()})

    async def _update_live(self, conv: Conversation, **changes: str) -> bool:
        controller = conv.controller
        if controller.state not in (SessionState.READY, SessionState.RUNNING):
            return False
        ok = await controller.update_settings(**changes)
        if not ok:
            self.say("设置没有生效，已恢复为当前使用的值。")
        conv.dirty = True
        self._schedule_flush()
        return ok

    def _set_pending_model(self, conv: Conversation, target: dict[str, Any] | None, *, row: str = "") -> None:
        # This intent is not a disposable UI cache or an engine setting. Reopening never executes it on its own.
        row = row or self._row_of(conv)
        if row and conv.in_history:
            try:
                self.history.save("pending-switch:" + row, {"project": conv.project, "turns": [], "target": target})
            except OSError as error:
                raise AppError("无法保存模型选择，消息没有发出；请检查存储后重试。") from error
        conv.pending_model = target
        conv.dirty = True
        self._schedule_flush()

    def _select_model(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None, over_ok: bool,
    ) -> dict[str, Any]:
        self._check_fusion_switch(conv)
        active_fusion = getattr(conv.controller, "_fusion", None) is not None
        if (page_model == "fusion" and active_fusion or page_model != "fusion" and not active_fusion
                and self._page_settings(conv).get("model") == page_model):
            self._set_pending_model(conv, None)
            return {"ok": True}
        policy = None
        if page_model == "fusion":
            policy, _, _ = self._fusion_selection(conv)
            destination = policy.main
        else:
            destination = page_model
        chosen = self.catalog.find(destination)
        if not chosen or chosen.get("disabled"):
            raise AppError("所选模型当前不可用，原模型和待切换选择保持不变。")
        target = self.catalog.target(destination)
        if target.engine != conv.engine or target.engine in ("devin", "factory"):
            carry = self._switch_carry(conv, destination, carry_ids, over_ok)
            if isinstance(carry, dict):
                return carry
        settings = self._open_settings(destination, self._page_settings(conv))
        if policy is not None:
            from desktop.core.fusion.reasoning import configured_effort
            try:
                effort = configured_effort(policy.reasoning, policy.main, chosen.get("efforts", ()))
            except ValueError as error:
                raise AppError("Fusion 主模型的推理档位不可用。") from error
            if effort is not None:
                settings["reasoning"] = effort
        if policy is not None:
            self._prefetch_profiles(policy.main, policy.light, *policy.workers)
        else:
            self._prefetch_profiles(destination)
        self._set_pending_model(conv, {"model": page_model, "settings": settings, "attachments": list(carry_ids or ()),
                                      "overLimit": over_ok, "policy": policy.to_json() if policy is not None else None})
        return {"ok": True}

    async def _apply_pending_model(self, conv: Conversation) -> dict[str, Any]:
        pending = conv.pending_model
        if pending is None:
            return {"ok": True}
        self._check_fusion_switch(conv)
        old_row = self._row_of(conv)
        previous_task = conv.switch_task
        conv.switch_task = asyncio.current_task()
        conv.dirty = True
        self._schedule_flush()
        try:
            result = await self._choose_model(conv, pending["model"], pending["attachments"], pending["overLimit"])
            if not result.get("ok"):
                raise AppError("所选模型未能接收交接；消息和待切换选择保留，请重试。")
            # The pending target may have its own reasoning scale. Do not modify the source model at selection time.
            if pending["model"] != "fusion" and pending["settings"].get("reasoning") != self._page_settings(conv).get("reasoning"):
                if not await self._update_live(conv, reasoning=pending["settings"].get("reasoning", "")):
                    raise AppError("目标模型的推理设置未生效，消息没有发出。")
            return result
        except asyncio.CancelledError:
            raise AppError("模型切换已停止；消息和待切换选择保留，未自动重试。") from None
        finally:
            try:
                if conv.in_history and self._row_of(conv) != old_row:
                    self._set_pending_model(conv, pending)
                    self.history.save("pending-switch:" + old_row, {"project": conv.project, "turns": [], "target": None})
            except OSError as error:
                raise AppError("无法保存模型切换，消息没有发出；请检查存储后重试。") from error
            finally:
                conv.switch_task = previous_task
                conv.ran = False  # A handoff is neither a completed user turn nor a reason to advance goals/queues.
                conv.dirty = True
                self._feed_usage(conv)
                self._schedule_flush()

    async def _compact_model_switch(self, conv: Conversation, page_model: str) -> str:
        """The old model owns the summary. No destination settings are committed before it succeeds."""
        if not conv.in_history:
            return ""  # no prompt was delivered to the source, so there is nothing to summarize
        from desktop.core.chatgpt.errors import ChatGPTError
        from desktop.core.model_switch import compact_native, recall_for_switch
        old = conv.controller
        window = int((self.catalog.find(page_model) or {}).get("window") or 0)
        previous_task = conv.switch_task
        conv.switch_task = asyncio.current_task()
        try:
            compact = getattr(old, "compact_for_switch", None)
            summary = await compact(window) if compact is not None else await compact_native(old, window)
            if summary and getattr(old, "saved", None) is not None and old.saved.compaction:
                memory = recall_for_switch(old.manager.memory, old.engine, old.project, old.transcript)
                if memory:
                    record = dict(old.saved.compaction)
                    record["continuity"] = (record.get("continuity", "") + "\n\n" + memory).strip()
                    old.saved.compaction = record
                # A model handoff is a new prompt boundary: rebuild AGENTS.md, mode and Fusion instructions for
                # the destination instead of retaining an obsolete top-level instruction snapshot.
                old.saved.prompt = {}
                old._save_now(required=True)
                await old.retire_acp_context()
            return summary
        except asyncio.CancelledError:
            raise AppError("模型切换已停止，原模型和历史保留。") from None
        except Exception as error:
            detail = str(error) if isinstance(error, ChatGPTError) else error_text(error)
            raise AppError(f"切换前压缩未完成，仍使用原模型：{detail}") from error
        finally:
            conv.switch_task = previous_task

    async def _choose_model(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None = None, over_ok: bool = False
    ) -> dict[str, Any]:
        if page_model == "fusion":
            return await self._enable_fusion(conv, carry_ids, over_ok)
        target = self.catalog.target(page_model)
        if self._page_settings(conv).get("model") == page_model and getattr(conv.controller, "_fusion", None) is None:
            return {"ok": True}
        self._check_fusion_switch(conv)
        chosen = self.catalog.find(page_model)
        if not chosen or chosen.get("disabled"):
            raise AppError("所选模型当前不可用，原模型保持不变。")
        if getattr(conv.controller, "_fusion", None) is not None:
            self._check_fusion_switch(conv)
            if target.engine == "chatgpt":
                settings, _ = settings_for_model(self.catalog, self._page_settings(conv), page_model)
                conv.switching = True
                try:
                    if conv.controller.model != target.model:
                        await self._compact_model_switch(conv, page_model)
                    conv.controller.disable_fusion(target.model, settings.get("reasoning", ""))
                    await conv.controller.release_inactive_acp()
                except ValueError as error:
                    raise AppError("无法退出 Fusion：所选模型不可用，原会话保持不变。") from error
                finally:
                    conv.switching = False
                    self._feed_usage(conv)
                    conv.dirty = True
                    self._schedule_flush()
                self.say("已退出 Fusion；历史和专家上下文保留，后续消息使用所选普通模型。")
                return {"ok": True}
        if target.engine != conv.engine or (target.engine in ("devin", "factory") and conv.controller.transcript.turns):
            plan = getattr(getattr(conv.controller, "saved", None), "plan", {})
            if isinstance(plan, dict) and plan.get("status") == "pending":
                raise AppError("计划仍待确认；请先执行或修改计划，再切换引擎。")
            return await self._switch_engine(conv, page_model, carry_ids, over_ok)
        current = self._page_settings(conv)
        settings, changed = settings_for_model(self.catalog, current, page_model)
        changes: dict[str, str] = {"model": target.model}
        if "reasoning" in changed and settings.get("reasoning"):
            changes["reasoning"] = settings["reasoning"]
        conv.switching = True
        try:
            await self._compact_model_switch(conv, page_model)
            ok = await self._update_live(conv, **changes)
        finally:
            conv.switching = False
            self._feed_usage(conv)
            conv.dirty = True
            self._schedule_flush()
        if ok:
            if "reasoning" in changed:
                model = self.catalog.find(page_model) or {}
                self.say(
                    f"{model.get('label', page_model)} 没有推理档位。"
                    if not settings.get("reasoning")
                    else f"{model.get('label', page_model)} 不支持「{current.get('reasoning', '')}」推理，已改为「{settings['reasoning']}」。"
                )
        self._prefetch_profiles(page_model)
        return {"ok": ok}

    def _check_fusion_switch(self, conv: Conversation) -> None:
        controller = conv.controller
        plan = getattr(getattr(controller, "saved", None), "plan", {})
        if (conv.switching or conv.queue or conv.goal is not None or controller.state is not SessionState.READY
                or getattr(controller, "_turn", None) is not None or self._attention_prompts(conv)):
            raise AppError("本轮还在运行或仍有排队消息、目标任务；请先结束，再切换模型或 Fusion。")
        if isinstance(plan, dict) and plan.get("status") == "pending":
            raise AppError("计划仍待确认；请先处理计划，再切换 Fusion。")

    def _retained_fusion(self, conv: Conversation) -> dict[str, Any] | None:
        controller = conv.controller
        runtime = getattr(controller, "_fusion", None) or getattr(controller, "_paused_fusion", None)
        if runtime is None:
            return (conv.history or {}).get("fusion")
        prefixes = tuple("agent:" + ident for ident in runtime.agents)
        def expert_series(values):
            return {key: tokens.to_json() for key, tokens in values.items()
                    if any(key == prefix or key.startswith(prefix + ":") for prefix in prefixes)}
        return {"owner": runtime.owner, "registry": runtime.registry.to_json(),
                "settings": controller.saved.origin.get("fusionSettings", runtime.settings_snapshot(runtime.registry.policy)),
                "mainTokens": runtime.main_tokens + (controller.usage.total if controller.usage is not None else 0),
                "seen": expert_series(conv.ledger.seen), "modelSeen": expert_series(conv.ledger.model_seen)}

    @staticmethod
    def _fusion_series(retained: Any, agents: Any) -> tuple[dict[str, Tokens], dict[str, Tokens]]:
        restored = ({}, {})
        if retained is None:
            return restored
        prefixes = tuple("agent:" + ident for ident in agents)
        for name, series in zip(("seen", "modelSeen"), restored):
            values = retained.get(name, {})
            if not isinstance(values, dict):
                raise AppError("保留的专家用量记录损坏，原会话未改变。")
            for key, raw in values.items():
                tokens = Tokens.of(raw)
                if (tokens is None or not isinstance(key, str)
                        or not any(key == prefix or key.startswith(prefix + ":") for prefix in prefixes)):
                    raise AppError("保留的专家用量记录损坏，原会话未改变。")
                series[key] = tokens
        return restored

    def _fusion_selection(self, conv: Conversation) -> tuple[Any, Any, Any]:
        from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
        retained = self._retained_fusion(conv)
        restored = ({}, {})
        try:
            if retained is not None:
                if not isinstance(retained, dict):
                    raise FusionError("Invalid retained Fusion state.")
                registry = FusionRegistry.from_json(Path(conv.project), retained.get("registry"))
                policy = registry.policy
                restored = self._fusion_series(retained, registry.agents)
            else:
                policy = Policy(self.settings.fusion_main, tuple(self.settings.fusion_workers), self.settings.fusion_light,
                                0, 0, self.settings.fusion_max_seconds, self.settings.fusion_preference,
                                self.settings.fusion_reasoning, self.settings.fusion_max_seconds_adaptive)
        except FusionError as error:
            raise AppError("Fusion 配置不可用；请检查设置或保留的专家配置，原会话未改变。") from error
        return policy, retained, restored

    # ------------------------------------------------------------------ model profiles (public leaderboard prefetch)

    def _profiles_fetch(self) -> Any:
        if self._profiles_source is None:
            from desktop.core.fusion import aa_catalog
            self._profiles_source = aa_catalog.source()
        return self._profiles_source

    def _profile_lookup(self) -> Callable[[str], Any]:
        """Match API entries on the actual model ID, not a user label or generated entry ID."""
        fetch = self._profiles_fetch()
        native_ids: dict[str, str] | None = None

        def lookup(model: str) -> Any:
            nonlocal native_ids
            if model.startswith("api:"):
                if native_ids is None:
                    lister = getattr(self.providers, "api_models", None)
                    native_ids = {spec.id: spec.native for spec in lister()} if lister is not None else {}
                model = native_ids.get(model, model)
            return fetch(model)

        return lookup

    async def _refresh_profiles(self) -> None:
        from desktop.core.fusion import profiles
        s = self.settings
        models = [model for model in (s.default_model, s.fusion_main, s.fusion_light, *s.fusion_workers)
                  if model.startswith(PREFIXES)]
        await asyncio.to_thread(profiles.refresh, default_settings_path().parent, models, self._profile_lookup())

    def _prefetch_profiles(self, *models: Any) -> None:
        """Selection never waits for the public catalog; changing effort doesn't need another lookup."""
        if self.offline:
            return
        now = time.monotonic()
        for model in models:
            if not isinstance(model, str) or not model.startswith(PREFIXES):
                continue
            if now - self._prefetched_profiles.get(model, -PREFETCH_DEDUPE_SECONDS) < PREFETCH_DEDUPE_SECONDS:
                continue
            self._prefetched_profiles[model] = now
            self._spawn(self._profile_task(model), quiet=True)

    async def _profile_task(self, model: str) -> None:
        from desktop.core.fusion import profiles
        try:
            await asyncio.to_thread(profiles.ensure, default_settings_path().parent, model, self._profile_lookup())
        except Exception:
            # A custom model id can be private; the engine prefix is enough.
            log.exception("prefetching a model profile for %s failed", model.partition(":")[0])

    def _prefetch_conv(self, conv: Conversation) -> None:
        fusion = getattr(conv.controller, "_fusion", None)
        if fusion is not None:
            policy = fusion.registry.policy
            self._prefetch_profiles(policy.main, policy.light, *policy.workers)
        else:
            self._prefetch_profiles(self._page_settings(conv).get("model"))

    async def _refresh_fusion_settings(self, conv: Conversation) -> None:
        """Adopt the global preset at a new-turn boundary, never in a running model/tool step."""
        from desktop.core.fusion.reasoning import configured_effort
        fusion = getattr(conv.controller, "_fusion", None)
        if fusion is None:
            return
        previous_task = conv.switch_task
        try:
            policy = fusion.pending_settings()
            if policy is None:
                return
            if conv.controller.saved.plan.get("status") == "pending":
                return  # Revising/approving that plan still belongs to its original main agent.
            prepared = fusion.prepare_settings(policy)
            info = self.chatgpt.model_info(policy.main.removeprefix("chatgpt:"))
            settings = self._open_settings(policy.main, self._page_settings(conv))
            effort = configured_effort(policy.reasoning, policy.main, info.efforts)
            conv.switching, conv.switch_task = True, asyncio.current_task()
            if policy.main != fusion.registry.policy.main:
                await self._compact_model_switch(conv, policy.main)
            fusion.check_budget()  # A source summary may exhaust a legacy limit or make its usage uncertain.
            fusion.apply_settings(prepared, effort if effort is not None else settings.get("reasoning", ""))
            await conv.controller.release_inactive_acp()
        except ValueError as error:
            raise AppError(f"Fusion 新配置未应用，消息没有发出；请检查模型和推理设置：{error}") from error
        finally:
            conv.switching, conv.switch_task = False, previous_task
            conv.dirty = True
            self._feed_usage(conv)
            self._schedule_flush()

    async def _enable_fusion(self, conv: Conversation, carry_ids: Sequence[str] | None, over_ok: bool) -> dict[str, Any]:
        from desktop.core.fusion.registry import FusionError
        self._check_fusion_switch(conv)
        if getattr(conv.controller, "_fusion", None) is not None:
            return {"ok": True}
        self._feed_usage(conv)
        policy, retained, restored = self._fusion_selection(conv)
        paused = getattr(conv.controller, "_paused_fusion", None)
        prepared = None
        if paused is not None:
            latest = paused.pending_settings()
            if latest is not None:
                prepared = paused.prepare_settings(latest)
                policy = latest
        model = policy.main.removeprefix("chatgpt:")
        if self.chatgpt is None or self.chatgpt.model_info(model) is None:
            raise AppError("Fusion 主模型当前不可用；不会自动替换模型。")
        settings = self._open_settings(policy.main, self._page_settings(conv))
        if conv.engine != "chatgpt":
            return await self._switch_engine(conv, policy.main, carry_ids, over_ok, fusion_policy=policy, fusion_retained=retained)
        conv.switching = True
        try:
            if conv.controller.model != model:
                await self._compact_model_switch(conv, policy.main)
            if prepared is not None:
                from desktop.core.fusion.reasoning import configured_effort
                effort = configured_effort(policy.reasoning, policy.main, self.chatgpt.model_info(model).efforts)
                paused.apply_settings(prepared, effort if effort is not None else settings.get("reasoning", ""))
            else:
                conv.controller.enable_fusion(policy, reasoning=settings.get("reasoning", ""), retained=retained)
        except FusionError as error:
            raise AppError("无法启用 Fusion：模型档位或保留的专家配置不可用，原会话未改变。") from error
        finally:
            conv.switching = False
            self._feed_usage(conv)
            conv.dirty = True
            self._schedule_flush()
        conv.ledger.seen.update(restored[0])
        conv.ledger.model_seen.update(restored[1])
        self._prefetch_profiles(policy.main, policy.light, *policy.workers)
        self.say("已在本会话启用 Fusion；历史和专家上下文保留，不会自动启动任务。")
        return {"ok": True}

    async def _window_of(self, controller: Any, page_model: str) -> int:
        """The context window (tokens) of the model a session was just opened with: the model list's own figure, else what
        the session reports a moment after opening it. 0: not known."""
        known = int((self.catalog.find(page_model) or {}).get("window") or 0)
        for _ in range(0 if known else WINDOW_WAIT_STEPS):
            known = int(getattr(getattr(controller, "context", None), "window", 0) or 0)
            if known:
                break
            await asyncio.sleep(WINDOW_WAIT_STEP)
        return known

    def _images_problem(self, page_model: str) -> str:
        """Why the model cannot take pictures (empty: it can, or nothing is known against it)."""
        target = self.catalog.target(page_model)
        lookup = getattr(getattr(self, target.engine, None), "model_info", None)
        info = lookup(target.model) if lookup is not None else None
        if isinstance(info, Mapping):
            if info.get("noImageSupport"):
                return f"「{info.get('displayName') or info.get('id') or target.model}」不支持图片输入。"
        elif info is not None and not info.supports_images:
            return f"「{info.display_name}」不支持图片输入。"
        return ""

    def _scans_problem(self, page_model: str) -> str:
        """Why a PDF without words cannot go to this model (empty: it can). Every engine reads only a PDF's words."""
        engine = self.catalog.engine_of(page_model)
        return f"{engine_label(engine, self.catalog.find(page_model))} 只能读 PDF 里的文字，这个 PDF 里没有提取到文字。"

    @staticmethod
    def _carry_json(option: CarryOption) -> dict[str, Any]:
        item = option.attachment
        size = len(attachment_text(item).encode("utf-8")) if option.mode == AS_TEXT else (item.size or len(item.data))
        out: dict[str, Any] = {"id": item.id, "name": item.name, "kind": item.kind, "as": option.mode, "bytes": size}
        if option.reason:
            out["reason"] = option.reason
        return out

    @staticmethod
    def _resolve_carry(options: Sequence[CarryOption], ids: Sequence[str]) -> list[Attachment]:
        """The attachments the user picked. A pick that cannot go is an error that says why: nothing is skipped quietly."""
        by_id = {option.attachment.id: option for option in options}
        chosen: list[Attachment] = []
        for attachment_id in dict.fromkeys(ids):
            option = by_id.get(attachment_id)
            if option is None:
                raise AppError("找不到选中的附件，对话可能已经变了：请重新选择。")
            if not option.available:
                raise AppError(f"「{option.attachment.name}」带不过去：{option.reason}")
            chosen.append(option.attachment)
        return chosen

    def _switch_carry(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None, over_ok: bool,
    ) -> list[Attachment] | dict[str, Any]:
        options = carry_options(conv.controller.transcript.turns, images_problem=self._images_problem(page_model),
                                scans_problem=self._scans_problem(page_model))
        in_box, in_box_bytes = len(conv.drafts), sum(item.size or 0 for item in conv.drafts)
        if options and carry_ids is None:
            return {"ok": False, "choose": {
                "attachments": [self._carry_json(option) for option in options],
                "limits": {"files": SOFT_ATTACHMENTS, "bytes": SOFT_TOTAL_BYTES},
                "hard": {"files": HARD_ATTACHMENTS, "bytes": HARD_TOTAL_BYTES},
                "drafts": {"count": in_box, "bytes": in_box_bytes},
            }}
        chosen = self._resolve_carry(options, carry_ids or ())
        held = carry_overage(chosen, other_count=in_box, other_bytes=in_box_bytes)
        if held.beyond_ceiling:
            raise AppError(too_many(held))
        if held.over and not over_ok:
            raise AppError(f"{held.describe()}：要这样带过去，请在清单里确认。")
        return chosen

    def _archive_sid_of(self, controller: Any) -> str | None:
        """The logical-session sid this runtime was bound to, if any — only its own bound state counts; an id the
        page or another party passes in is never taken as an identity. A native Devin session reopened from
        history has lost the in-memory sid, so the archive's own index answers for it, by the session id its
        endpoint returned: only an ordinary Devin instance counts, a missing index means "never archived"
        (nothing is created), and an unreadable one raises so the switch rolls back instead of forking a second sid."""
        binding = getattr(controller, "_archive_binding", None)
        if binding is not None:
            return binding.archive.sid
        sid = getattr(controller, "_archive_sid", None)
        if isinstance(sid, str) and sid:
            return sid
        origin = getattr(getattr(controller, "saved", None), "origin", None)
        if isinstance(origin, dict):
            return origin.get("sessionArchive")
        native = getattr(controller, "session_id", None)
        if getattr(controller, "engine", "") != "devin" or not isinstance(native, str) or not native:
            return None
        from desktop.core.session_archive import INDEX_NAME, resolve_local

        root = self.session_archive_root()
        if not (root / INDEX_NAME).is_file():
            return None
        owner = resolve_local(root, "devin", native)
        return owner["sid"] if owner is not None and owner["backend"] == "devin" else None

    def _adopt_archive(self, old: Any, new: Any) -> None:
        """Hand the trusted logical-session sid to the freshly opened controller, so an engine switch stays one
        archive: a GPT/harness segment rebinds through ``ArchiveBinding.restore``; a native Devin session enters
        with the session id its endpoint actually returned (re-entering the same native id resumes its ``iid``).
        Let this run only inside the switch's guard: a failure rolls the switch back — it must never graft a
        wrong identity or invent a second sid beside the trusted one."""
        sid = self._archive_sid_of(old)
        if sid is None:
            return
        if new.engine == "devin":
            from desktop.core.session_archive import SessionArchive

            archive = SessionArchive.open(self.session_archive_root(), sid, meter=self.archive_meter())
            archive.enter("devin", "devin", new.session_id)
            new._archive, new._archive_sid = archive, sid
            return
        if new.engine != "chatgpt":
            return
        from desktop.core.chatgpt.archive_binding import ArchiveBinding
        from desktop.core.chatgpt.controller import ARCHIVE_ORIGIN

        binding = ArchiveBinding.restore(new.manager, sid, new.session_id)
        new._archive_adopt(binding)
        new.saved.origin[ARCHIVE_ORIGIN] = sid
        new._archive_pending = False

    async def _switch_engine(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None = None, over_ok: bool = False,
        *, fusion_policy: Any = None, fusion_retained: Any = None,
    ) -> dict[str, Any]:
        """Continue this conversation with the other engine: a new native session, the earlier text handed over.

        The new session is opened first; only when it is ready does it replace the old one, so a failure (not signed
        in, no Devin CLI ...) leaves the conversation exactly as it was.

        Attachments go along only when the user picks them, one by one. When the conversation holds any, nothing
        happens until the page says which (``carry_ids``): the answer is ``{"ok": False, "choose": {...}}`` listing every
        attachment and whether and how it could go (and why not), the page asks, then calls again with the ids picked
        (an empty list: none). A pick that makes the first message hold more than a message usually does is the user's
        call: the page shows by how much and says so (``over_ok``); only without that is it an error.
        """
        old = conv.controller
        old_row = self._row_of(conv)
        previous_title = self._title_of(conv)
        if conv.switching:
            raise AppError("模型正在切换，稍后再试。")
        if old.busy:
            raise AppError("本轮还在运行：结束后才能换引擎。")
        if old.state is not SessionState.READY:
            raise AppError("会话还没有就绪，稍后再换引擎。")
        model = self.catalog.find(page_model) or {"label": page_model, "window": 0}
        was = self.catalog.find(self._page_settings(conv).get("model", "")) or {"label": old.model}
        transcript = old.transcript
        chosen = self._switch_carry(conv, page_model, carry_ids, over_ok)
        if isinstance(chosen, dict):
            return chosen
        # What goes along depends on the new model's window. The model list usually knows it; where it does not, it is
        # only reported once the session is open, so the session opens with a hand-over for the figure known now
        # (nothing may be sent without one) and gets the final one when the window arrives.
        window = int(model.get("window") or 0)
        prior_turns = [t for t in transcript.turns if t.id not in conv.unsent_turns]
        conv.switching = True
        try:
            summary = await self._compact_model_switch(conv, page_model)
        finally:
            conv.switching = False
            self._feed_usage(conv)
            conv.dirty = True
            self._schedule_flush()
        from desktop.core.model_switch import summary_handoff
        sent_as_files = any(not attachment_text(item).strip() for item in chosen)
        handoff = (
            summary_handoff(summary, transcript, project=conv.project, memory=getattr(old.manager, "memory", None),
                            engine=self.catalog.target(page_model).engine, window=window, carry=chosen)
            if summary else build_handoff(prior_turns, window_tokens=window, carry=chosen)
            if sent_as_files or conversation_messages(prior_turns, carry={item.id for item in chosen}) else None
        )
        settings = self._open_settings(page_model, {**self._page_settings(conv),
                                                   **({"autonomy": ""} if self.catalog.target(page_model).engine != conv.engine else {})})
        if fusion_policy is not None and settings.get("autonomy") in ("plan", "smart"):
            if settings["autonomy"] == "plan":
                settings["mode"], settings["autonomy"] = "spec", "ask"
            else:
                settings["autonomy"] = "code"
        previous_requested = conv.requested
        before = transcript.snapshot()
        # What the old session counted is final: the total keeps it, and the new session's figures (it counts from zero)
        # are new ones. A figure the shared transcript keeps goes too, or the new session would start from the one an
        # earlier session left there.
        self._feed_usage(conv)
        retained = self._retained_fusion(conv)
        ledger_before = (conv.ledger.total, dict(conv.ledger.seen), dict(conv.ledger.model_seen))
        usage_before = transcript.usage
        conv.ledger.restart()
        transcript.usage = None
        conv.requested = dict(settings)
        conv.switching = True
        self._adopt = conv
        controller: Any = None
        try:
            if fusion_policy is not None:
                controller = await self.chatgpt.open_new(conv.project, model=page_model.removeprefix("chatgpt:"),
                                                         reasoning=settings.get("reasoning", ""), autonomy=settings.get("autonomy", "code"),
                                                         mode=settings.get("mode", "auto"), transcript=transcript, handoff=handoff)
                controller.enable_fusion(fusion_policy, reasoning=settings.get("reasoning", ""), retained=fusion_retained)
                restored = self._fusion_series(fusion_retained, controller.agents)
                conv.ledger.seen.update(restored[0])
                conv.ledger.model_seen.update(restored[1])
            else:
                controller = await self._open_controller(
                    conv.project, page_model, settings, transcript=transcript, handoff=handoff, replacing=True
                )
            if controller.state is not SessionState.READY:
                raise AppError(controller.last_error or "没有切换成功：新的会话没有启动。")
            if controller.engine != "devin":
                # A GPT/harness segment's sid must join ``saved.origin`` before the compatibility snapshot below.
                self._adopt_archive(old, controller)
            if handoff is not None and not window and not summary:
                window = await self._window_of(controller, page_model)
                if window and getattr(controller, "handoff", None) is handoff and controller.state is SessionState.READY:
                    handoff = controller.handoff = build_handoff(prior_turns, window_tokens=window, carry=chosen)
            if handoff is not None and handoff.carried:
                transcript.add_switch(
                    from_model=was.get("label", ""), from_engine="Fusion" if getattr(old, "_fusion", None) is not None else engine_label(old.engine, was),
                    to_model=model["label"], to_engine="Fusion" if fusion_policy is not None else engine_label(controller.engine, model),
                    carried=handoff.carried, total=handoff.total, size=handoff.size, truncated=handoff.truncated,
                )
            new_row = self._row_of(conv)
            history = {"project": conv.project, "title": previous_title, "activeAt": old.active_at, "created": conv.created_at,
                       "turns": [t for t in transcript.turns if t.id not in conv.unsent_turns],
                       "ledger": conv.ledger.to_json(), "handoff": handoff}
            if retained is not None:
                history["fusion"] = retained
            if conv.in_history:
                await asyncio.to_thread(self.history.save, new_row, history)
                if controller.engine == "chatgpt":
                    # Preserve even an empty native segment: its previous turns are in the engine-neutral archive.
                    await asyncio.to_thread(self.chatgpt.sessions.save, controller._snapshot())
                    controller._checkpoint_written = True
            if controller.engine == "devin":
                # A Devin ``enter`` writes the archive's timeline and current at once: last, after every other
                # fallible write, so a failed checkpoint never leaves the archive switched to a lost segment.
                self._adopt_archive(old, controller)
        except BaseException as error:
            self._rollback_switch(conv, old, controller)
            transcript.restore(before)  # a failed open writes its error into the shared transcript
            conv.ledger.total, conv.ledger.seen, conv.ledger.model_seen = ledger_before
            transcript.usage = usage_before
            conv.requested = previous_requested
            if isinstance(error, (AppError, asyncio.CancelledError)):
                raise
            raise AppError(f"没有切换成功：{error_text(error)}") from error
        finally:
            conv.switching = False
            self._adopt = None
        if conv.in_history:
            if old.session_id:
                conv.hidden.add(old.session_id)
            aliases = [key for key in self.settings.session_links if self._resolve_row(key) == old_row]
            for key in [*aliases, old_row]:
                if key and key != new_row:
                    self.settings.session_links[key] = new_row
            for names in (self.settings.session_titles, self.settings.session_auto_titles):
                if old_row in names:
                    names[new_row] = names.pop(old_row)
            conv.history = history
            self._sync_sidebar_key(conv)
            self._save()
        transcript.model = controller.model
        self._spawn(old.close(), quiet=True)
        self._emit_sidebar()
        conv.meta = {}
        conv.dirty = True
        self._flush(conv)
        note = (f"已在本会话启用 Fusion，主模型为 {model['label']}。" if fusion_policy is not None
                else f"已换到 {model['label']}（{engine_label(controller.engine, model)}）。")
        if handoff is not None and not handoff.empty:
            if handoff.carried:
                note += f"之前 {handoff.carried} 条对话的文字会随下一条消息交给它。"
            along = []
            if handoff.files:
                along.append(f"{handoff.files} 个文本附件的内容")
            if handoff.pictures:
                along.append(f"{handoff.pictures} 张图片")
            if handoff.pdfs:
                along.append(f"{handoff.pdfs} 个 PDF 文件")
            if along:
                note += "、".join(along) + "一起带过去。"
            if handoff.clipped:
                note += f"其中 {handoff.clipped} 条太长，只带了开头。"
        self.say(note)
        return {"ok": True, "switched": True}

    def _rollback_switch(self, conv: Conversation, old: Any, new: Any) -> None:
        """The new session did not open: the conversation goes back to the old one, the new one is closed."""
        adopted = conv.controller
        if adopted is not old:
            self._by_controller.pop(adopted, None)
            self._retired.add(adopted)
            self._by_controller[old] = conv
            conv.controller = old
            self._retired.discard(old)
            self._spawn(adopted.close(), quiet=True)
        elif new is not None and new is not old:
            self._retired.add(new)
            self._spawn(new.close(), quiet=True)
        conv.dirty = True
        self._schedule_flush()

    # ------------------------------------------------------------------ the shared memory

    def _no_memory(self) -> MemoryBridge:
        if self.memory is None:
            raise AppError("这个版本没有共享记忆。")
        return self.memory

    def _no_imports(self) -> ImportBridge:
        if self.imports is None:
            raise AppError("这个版本不能从别的应用导入。")
        return self.imports

    def _import_defaults(self) -> dict[str, str]:
        """What an imported conversation goes on with: a ChatGPT Plan model, and the permission new ones get."""
        settings = self.settings
        model = settings.default_model
        chatgpt_model = ""
        if self.catalog is not None:
            if model and self.catalog.source_of(model) == "chatgpt":
                chatgpt_model = model
            else:
                chatgpt_model = next(
                    (item["id"] for item in self.catalog.models if item["source"] == "chatgpt" and not item.get("disabled")),
                    "",
                )
        return {"model": chatgpt_model, "reasoning": "", "autonomy": settings.chatgpt_autonomy}

    async def _import_call(self, work: Any, params: dict[str, Any]) -> dict[str, Any]:
        bridge = self._no_imports()
        return await asyncio.to_thread(getattr(bridge, work), params)

    async def _m_import_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._import_call("scan", params)

    async def _m_import_memories(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._import_call("memories", params)

    async def _m_import_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._import_call("sessions_list", params)

    async def _m_import_commit(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self._import_call("commit", params)
        projects = result.pop("projects", [])
        added = False
        for folder in reversed(projects):  # new projects lead the list: keep the import's own order there
            added = self.settings.add_project(folder) or added
        if added:
            self._save()
            self.emit("settings", {"settings": self.settings_view()})
        for folder in projects:
            self._schedule_sidebar(folder, reload=True)  # (the new conversation is in the store: read the folder again)
        return {**result, **self._no_memory().state()}

    async def _m_import_undo(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self._import_call("undo", params)
        return {**result, **self._no_memory().state()}

    async def _memory_call(self, work: Any, params: dict[str, Any]) -> dict[str, Any]:
        memory = self._no_memory()
        try:
            return await asyncio.to_thread(getattr(memory, work), params)
        except MemoryRejected as rejected:
            raise AppError(explain(rejected)) from None

    async def _m_memory_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("list", params)

    async def _m_memory_changes(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("changes", params)

    async def _m_memory_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("create", params)

    async def _m_memory_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("update", params)

    async def _m_memory_forget(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("forget", params)

    async def _m_memory_restore(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("restore", params)

    async def _m_memory_undo(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("undo", params)

    def _migrate_suggestions(self) -> None:
        """Suggestions that were waiting for the user's word before the application wrote notes on its own become notes
        now, and join the first review (nothing is lost, nothing is asked one by one any more)."""
        memory = self.memory
        if memory is None:
            return
        try:
            migrate_proposals(memory.proposals, memory.store, memory.review, memory.learner)
        except Exception:
            log.warning("the suggestions from before could not be moved to the review", exc_info=True)

    async def _m_memory_review(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._memory_call("review_batch", params)

    async def _m_memory_reviewApply(self, params: dict[str, Any]) -> dict[str, Any]:
        done = await self._memory_call("review_apply", params)
        self.emit("memory", {key: done[key] for key in ("latest", "unseen", "seen", "pending")})
        return done

    # ------------------------------------------------------------------ what a turn leaves for the memory

    def _curate(self, conv: Conversation) -> None:
        """A turn has just ended: show the curator what was said (once per turn, and only a turn that went well)."""
        if getattr(conv.controller, "_fusion", None) is not None:
            return  # Do not make an unbudgeted side-model call from a Fusion session.
        curator = self.curator
        turns = conv.controller.transcript.turns
        if curator is None or not turns or self._shutting_down:
            return
        turn = turns[-1]
        if turn.status is not TurnStatus.DONE or turn.id in conv.curated:
            return
        conv.curated.add(turn.id)
        controller = conv.controller
        if conv.engine == "chatgpt" and is_api_model(str(getattr(controller, "model", ""))):
            return  # (what was said to an API provider's model is not sent to the ChatGPT plan to be sorted: another company)
        native = getattr(controller, "session_id", None) or ""
        exchange = Exchange(
            engine=conv.engine,
            project=conv.project,
            conversation=controller.title or "",
            row=row_id(conv.engine, native) if native else "",
            user=turn.user_block.text if turn.user_block is not None else "",
            assistant="\n\n".join(block.text for block in turn.blocks if block.kind == "text" and block.text),
            key=conv.cid,
        )
        if curator.worth_asking(exchange):
            self._spawn(curator.consider(exchange), quiet=True)

    def _can_ask(self, engine: str) -> bool:
        """Whether this engine can be asked outside its conversations at all (``_extract_for_memory``). A turn of an engine
        that cannot is not offered to the curator: it would take the hour's allowance and return nothing."""
        return (engine == "chatgpt" and self.chatgpt is not None) or (engine == "devin" and self.devin is not None)

    async def _extract_for_memory(self, engine: str, project: str, prompt: str) -> str | None:
        """One question to the engine of the conversation, outside it. Only an engine that can be asked without leaving a
        trace in the user's own lists answers; the others are not asked (and the text is never sent to a different
        provider than the one the user chose for that conversation)."""
        if engine == "chatgpt" and self.chatgpt is not None:
            return await chatgpt_side.ask(self.chatgpt, prompt)
        if engine == "devin" and self.devin is not None:
            return await devin_side.ask(self.devin, project, prompt)
        return None

    def _suggestions_changed(self) -> None:
        self._spawn(self._announce_memory(), quiet=True)

    async def _m_memory_markSeen(self, params: dict[str, Any]) -> dict[str, Any]:
        memory = self._no_memory()
        seq = params.get("seq")
        state = await asyncio.to_thread(memory.mark_seen, seq if isinstance(seq, int) and not isinstance(seq, bool) else None)
        self.emit("memory", state)
        return state

    def _note_run(self, conv: Conversation) -> None:
        """Tell the memory which conversation this run of an engine is (so that a note it wrote can say whose it is)."""
        memory = self.memory
        controller = conv.controller
        run = getattr(controller, "run", "")
        if memory is None or not run:
            return
        if memory.note_run(run, conv.engine, getattr(controller, "session_id", None) or "", controller.title, conv.project) and self._runs_handle is None:
            with contextlib.suppress(RuntimeError):
                self._runs_handle = asyncio.get_running_loop().call_later(1.0, self._write_runs)

    def _write_runs(self) -> None:
        self._runs_handle = None
        if self.memory is not None and not self._shutting_down:
            self._spawn(asyncio.to_thread(self.memory.flush_runs), quiet=True)

    def _watch_memory(self) -> None:
        """Tell the page when the memory changes (an agent wrote, the user did elsewhere): one ``stat`` a second."""
        self._memory_handle = None
        memory = self.memory
        if memory is None or self._shutting_down:
            return
        try:
            version = memory.store.version()
            if self._memory_state is not None and version != self._memory_state:
                self._spawn(self._announce_memory(), quiet=True)
            self._memory_state = version
        finally:
            with contextlib.suppress(RuntimeError):
                self._memory_handle = asyncio.get_running_loop().call_later(MEMORY_POLL_SECONDS, self._watch_memory)

    async def _announce_memory(self) -> None:
        if self.memory is not None:
            self.emit("memory", await asyncio.to_thread(self.memory.state))

    # ------------------------------------------------------------------ application settings

    async def _m_settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        key, value = params["key"], params["value"]
        s = self.settings
        seats = False
        if key == "theme" and value in THEMES:
            s.theme = value
        elif key == "fontScale" and value in FONT_SCALES:
            s.font_scale = value
        elif key == "followUp" and value in FOLLOW_UPS:
            s.follow_up = value
        elif key == "memoryEnabled" and isinstance(value, bool):
            s.memory_enabled = value
        elif key == "memoryRecall" and isinstance(value, bool):
            s.memory_recall = value
        elif key == "memoryCapture" and isinstance(value, bool):
            s.memory_capture = value
        elif key == "memoryCurate" and isinstance(value, bool):
            s.memory_curate = value
        elif key == "memoryEngines" and isinstance(value, list) and all(isinstance(name, str) for name in value):
            s.memory_engines = [name for name in MEMORY_ENGINES if name in value]
        elif key == "compactAuto" and isinstance(value, bool):
            s.chatgpt_compact = value
        elif key == "compactAt" and _is_int(value) and COMPACT_AT_RANGE[0] <= value <= COMPACT_AT_RANGE[1]:
            s.chatgpt_compact_at = value
        elif key == "maxSteps" and _is_int(value) and MAX_STEPS_RANGE[0] <= value <= MAX_STEPS_RANGE[1]:
            s.max_steps = value
        elif key == "reconnectInterval" and _is_int(value) and RECONNECT_INTERVAL_RANGE[0] <= value <= RECONNECT_INTERVAL_RANGE[1]:
            s.reconnect_interval = value
        elif key == "reconnectLimit" and _is_int(value) and RECONNECT_LIMIT_RANGE[0] <= value <= RECONNECT_LIMIT_RANGE[1]:
            s.reconnect_limit = value
        elif key in ("fusionMain", "fusionLight") and isinstance(value, str) and len(value) <= 512 and (not value or value.startswith(PREFIXES)):
            setattr(s, "fusion_main" if key == "fusionMain" else "fusion_light", value)
            seats = True
        elif key == "fusionWorkers" and isinstance(value, list) and len(value) <= 1 and all(isinstance(item, str) and len(item) <= 512 and item.startswith(PREFIXES) for item in value):
            s.fusion_workers = list(dict.fromkeys(value))
            seats = True
        elif key == "fusionReasoning":
            from desktop.core.fusion.reasoning import reasoning_map, reasoning_model
            try:
                levels = reasoning_map(value)
            except ValueError as error:
                raise AppError("Fusion 思考等级设置不合法。") from error
            for key, effort in levels.items():
                model = reasoning_model(key)
                if s.fusion_reasoning.get(key, s.fusion_reasoning.get(model)) == effort:
                    continue  # Keep older selections even if their model is temporarily unavailable.
                chosen = self.catalog.find(model)
                if chosen is None or effort not in chosen.get("efforts", ()):
                    raise AppError("所选模型不支持这个思考等级。")
            s.fusion_reasoning = levels
        elif key == "fusionPreference" and _is_int(value) and 0 <= value <= 100:
            s.fusion_preference = value
        elif key == "fusionMaxCalls" and _is_int(value) and 1 <= value <= 1000:
            s.fusion_max_calls = value
        elif key == "fusionMaxTokens" and _is_int(value) and 1 <= value <= 2**53 - 1:
            s.fusion_max_tokens = value
        elif key == "fusionMaxSeconds":
            pair = {"seconds": value, "adaptive": False} if _is_int(value) else value
            if not (isinstance(pair, Mapping) and set(pair) == {"seconds", "adaptive"} and type(pair["adaptive"]) is bool
                    and _is_int(pair["seconds"]) and 1 <= pair["seconds"] <= 86_400):
                raise AppError("这个设置的值不合法。")
            s.fusion_max_seconds, s.fusion_max_seconds_adaptive = pair["seconds"], pair["adaptive"]
        elif key == "fusionSteps":
            pair = value if isinstance(value, Mapping) else {}
            low, high = pair.get("min"), pair.get("max")
            low_adaptive, high_adaptive = pair.get("minAdaptive"), pair.get("maxAdaptive")
            valid = (set(pair) == {"min", "minAdaptive", "max", "maxAdaptive"}
                     and _is_int(low) and _is_int(high)
                     and STEP_BUDGET_RANGE[0] <= low <= STEP_BUDGET_RANGE[1]
                     and STEP_BUDGET_RANGE[0] <= high <= STEP_BUDGET_RANGE[1]
                     and type(low_adaptive) is bool and type(high_adaptive) is bool
                     # A disabled input never counts as inverted; a both-manual inversion is refused whole.
                     and (low_adaptive or high_adaptive or low <= high))
            if not valid:
                raise AppError("这个设置的值不合法。")
            s.fusion_steps_min, s.fusion_steps_min_adaptive = low, low_adaptive
            s.fusion_steps_max, s.fusion_steps_max_adaptive = high, high_adaptive
        elif key == "thinkChinese" and isinstance(value, bool):
            s.think_chinese = value
            s.thinking_language = "zh-CN" if value else "auto"
        elif key in ("answerLanguage", "thinkingLanguage") and value in LANGUAGES:
            if key == "answerLanguage":
                s.answer_language = value
            else:
                s.thinking_language = value
                s.think_chinese = value == "zh-CN"
        elif key == "numberedReply" and isinstance(value, bool):
            s.numbered_reply = value
        elif key == "soundEnabled" and isinstance(value, bool):
            s.sound_enabled = value
            if not value:
                self.platform.stop_sound()
        elif key in ("soundQuestion", "soundComplete", "soundError") and isinstance(value, str):
            kind = key[5:].lower()
            try:
                path = await asyncio.to_thread(sounds.validate_path, value) if value.strip() else ""
            except sounds.SoundProblem as error:
                raise AppError(str(error)) from error
            setattr(s, "sound_" + kind, path)
        elif key == "maxOpen" and _is_int(value) and MAX_OPEN_SESSIONS_RANGE[0] <= value <= MAX_OPEN_SESSIONS_RANGE[1]:
            s.max_open_sessions = value
        elif key == "sidebarWidth" and _is_int(value) and SIDEBAR_WIDTH_RANGE[0] <= value <= SIDEBAR_WIDTH_RANGE[1]:
            s.sidebar_width = value
        elif key == "conversationWidth" and _is_int(value) and CONVERSATION_WIDTH_RANGE[0] <= value <= CONVERSATION_WIDTH_RANGE[1]:
            s.conversation_width = value
        elif key == "inspectorWidth" and valid_inspector_width(value):
            s.inspector_width = value
        elif key == "devinPath" and isinstance(value, str):
            changed = value.strip() != s.devin_path
            s.devin_path = value.strip()
            if changed and self.devin is not None:
                self._spawn(self._devin_path_changed(), quiet=True)
        else:
            raise AppError("这个设置的值不合法。")
        if seats:
            self._prefetch_profiles(s.fusion_main, s.fusion_light, *s.fusion_workers)
        self._save()
        view = self.settings_view()
        self.emit("settings", {"settings": view})
        return view

    async def _m_storage_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.storage_view()

    async def _m_storage_set(self, params: dict[str, Any]) -> dict[str, Any]:
        key, value = params.get("key"), params.get("value")
        s = self.settings
        if key == "maxBytes" and _is_int(value) and storage_policy.MAX_BYTES_RANGE[0] <= value <= storage_policy.MAX_BYTES_RANGE[1]:
            s.storage_max_bytes = value
        elif key == "shares":
            shares = storage_policy.shares_from(value)
            if shares is None:
                raise AppError("各类占比必须合计 100%。")
            s.storage_shares = shares
        elif key == "alertAt" and _is_int(value) and storage_policy.ALERT_RANGE[0] <= value <= storage_policy.ALERT_RANGE[1]:
            s.storage_alert_at = value
        elif key == "fullPolicy" and value in storage_policy.FULL_POLICIES:
            s.storage_full_policy = value
        elif key == "autoclean":
            picks = storage_policy.autoclean_from(value)
            if picks is None:
                raise AppError("这个设置的值不合法。")
            s.storage_autoclean = picks
        else:
            raise AppError("这个设置的值不合法。")
        self._save()
        return self.storage_view()

    async def _m_sounds_pick(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        if not isinstance(kind, str) or kind not in sounds.KINDS:
            raise AppError("没有这种提示音。")
        start = sounds.sound_path(kind, getattr(self.settings, "sound_" + kind))
        path = await self.platform.pick_sound(start)
        if path is None:
            return self.settings_view()
        return await self._m_settings_set({"key": "sound" + kind.title(), "value": path})

    async def _m_sounds_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        if not isinstance(kind, str) or kind not in sounds.KINDS:
            raise AppError("没有这种提示音。")
        if not self.settings.sound_enabled:
            return {"ok": False}
        try:
            path = await asyncio.to_thread(sounds.validate_path, sounds.sound_path(kind, getattr(self.settings, "sound_" + kind)))
            if not self.settings.sound_enabled or self._shutting_down:
                return {"ok": False}
            self.platform.play_sound(path)
        except (sounds.SoundProblem, OSError) as error:
            raise AppError("提示音无法播放，请检查声音路径。") from error
        return {"ok": True}

    async def _devin_path_changed(self) -> None:
        """A new Devin program: read its account and models again, and drop the old process unless sessions use it."""
        stopped = await self.devin.path_changed()
        if not stopped:
            self.say("Devin 的路径已改：已打开的 Devin 会话还在用原来的程序，把它们关掉之后新路径才会生效。")
        self.emit("accounts", {"devin": await self._devin_account()})
        self._schedule_models()

    async def _m_models_setVisible(self, params: dict[str, Any]) -> dict[str, Any]:
        """Show or hide models in the conversation's model menu (the user's pick, per page model id)."""
        ids, visible = params.get("ids"), params.get("visible")
        if not isinstance(visible, bool) or not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise AppError("这个设置的值不合法。")
        hidden = list(self.settings.hidden_models)
        if visible:
            gone = set(ids)
            hidden = [i for i in hidden if i not in gone]
        else:
            hidden += [i for i in dict.fromkeys(ids) if i not in hidden]
        self.settings.hidden_models = hidden
        self._save()
        view = self.settings_view()
        self.emit("settings", {"settings": view})
        self.emit("defaults", {"defaults": self.defaults()})  # the default model may have just been hidden (or unhidden)
        return view

    # ------------------------------------------------------------------ accounts and providers


    async def _m_devin_account(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._devin_account()

    async def _devin_call(self, name: str) -> dict[str, Any]:
        if self.devin is None:
            raise AppError("没有找到 Devin 命令行：先安装 Devin 桌面版或 Devin CLI。")
        try:
            await getattr(self.devin, name)()
        except Exception as error:
            raise AppError(devin_error_text(error)) from error
        account = await self._devin_account()
        self.emit("accounts", {"devin": account})
        self._schedule_models()
        return account

    async def _m_devin_login(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._devin_call("login")

    async def _m_devin_cancelLogin(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._devin_call("cancel_login")

    async def _m_devin_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._devin_call("logout")

    def _connection_project(self, params: dict[str, Any]) -> str:
        project = params.get("projectId", "")
        if not isinstance(project, str):
            raise AppError("项目路径不合法。")
        if not project:
            return ""
        folder = normalize_folder(project)
        if not any(same_folder(folder, existing) for existing in self.settings.projects):
            raise AppError("请先把这个文件夹添加为项目。")
        return folder

    def _rules_path(self, params: dict[str, Any]) -> Path:
        project = self._connection_project(params)
        return Path(project) / "AGENTS.md" if project else customization.global_rules_path()

    async def _m_customization_read(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(customization.read_rules, self._rules_path(params))
        except (OSError, customization.RulesError) as error:
            raise AppError(str(error) if isinstance(error, customization.RulesError) else "规则文件不可读，请检查权限。") from error

    async def _m_customization_save(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(customization.save_rules, self._rules_path(params), params.get("text"), params.get("revision"))
        except (OSError, customization.RulesError) as error:
            raise AppError(str(error) if isinstance(error, customization.RulesError) else "规则文件未保存，请检查权限。") from error

    async def _m_connectors_list(self, params: dict[str, Any]) -> list:
        try:
            return await asyncio.to_thread(self.connectors.views)
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_connectors_configure(self, params: dict[str, Any]) -> list:
        try:
            if isinstance(params.get("config"), dict) and params["config"].get("enabled") is False:
                self.connectors.cancel(params.get("provider"))
            return await asyncio.to_thread(self.connectors.configure, params.get("provider"), params.get("config"))
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_connectors_login(self, params: dict[str, Any]) -> list:
        try:
            return await self.connectors.login(params.get("provider"), self.platform.open_url)
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_connectors_cancel(self, params: dict[str, Any]) -> list:
        try:
            self.connectors.cancel(params.get("provider"))
            return self.connectors.views()
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_connectors_disconnect(self, params: dict[str, Any]) -> list:
        try:
            self.connectors.cancel(params.get("provider"))
            return await asyncio.to_thread(self.connectors.disconnect, params.get("provider"))
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_marketplace_list(self, params: dict[str, Any]) -> dict:
        try:
            return await self.skill_marketplace.catalog(params.get("source"), refresh=params.get("refresh") is True)
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_marketplace_preview(self, params: dict[str, Any]) -> dict:
        try:
            return await self.skill_marketplace.preview(params.get("source"), params.get("name"))
        except PluginError as error:
            raise AppError(str(error)) from error

    async def _m_marketplace_install(self, params: dict[str, Any]) -> dict:
        try:
            return await asyncio.to_thread(self.skill_marketplace.install, params.get("preview"), confirmed=params.get("confirmed") is True)
        except (PluginError, ConnectionError) as error:
            raise AppError(str(error)) from error

    async def _m_marketplace_cancel(self, params: dict[str, Any]) -> dict:
        return await asyncio.to_thread(self.skill_marketplace.cancel, params.get("preview"))

    async def _m_connections_addMcp(self, params: dict[str, Any]) -> dict:
        try:
            return await asyncio.to_thread(self.connections.add_mcp, params.get("name", ""), params.get("configText"), self._connection_project(params), confirmed=params.get("confirmed") is True)
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_connections_list(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            scan = await asyncio.to_thread(self.connections.scan, self._connection_project(params))
            return {**scan, "builtinHooks": self.hooks.builtin_prompt_hooks()}
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_connections_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.connections.preview, params.get("path", ""), self._connection_project(params))
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_connections_import(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.connections.commit, params.get("preview"), params.get("confirmed"))
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_connections_update(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.connections.update, params.get("id"), enabled=params.get("enabled"), config=params.get("config"), confirmed=params.get("confirmed") is True, project=self._connection_project(params))
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_connections_test(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.connections.test(params.get("id"), self._connection_project(params))
        except ConnectionError as error:
            raise AppError(str(error)) from error

    async def _m_factory_account(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.factory is None:
            return {"cli": "missing", "cliPath": "", "enabled": False, "path": self.settings.factory_path}
        return {**await self.factory.account(), "path": self.settings.factory_path}

    async def _m_factory_configure(self, params: dict[str, Any]) -> dict[str, Any]:
        path, enabled = params.get("path"), params.get("enabled")
        if not isinstance(path, str) or not isinstance(enabled, bool):
            raise AppError("Factory 路径或启用状态不合法。")
        self.settings.factory_path, self.settings.factory_enabled = path.strip(), enabled
        self._save()
        if self.factory is not None:
            await self.factory.path_changed()
        self._schedule_models()
        for folder in self.settings.projects:
            self._spawn(self._reload_project(folder), quiet=True)
        return await self._m_factory_account({})

    async def _m_devin_setEnabled(self, params: dict[str, Any]) -> dict[str, Any]:
        self.settings.devin_enabled = bool(params["on"])
        self._save()
        account = await self._devin_account()
        self.emit("accounts", {"devin": account})
        self._schedule_models()
        for folder in self.settings.projects:
            self._spawn(self._reload_project(folder), quiet=True)
        return account

    async def _m_chatgpt_account(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._chatgpt_account()

    async def _chatgpt_call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.chatgpt is None:
            raise AppError("ChatGPT Plan 在这个版本里不可用。")
        try:
            await getattr(self.chatgpt, name)(*args, **kwargs)
        except ChatGPTError as error:
            self.emit("accounts", {"chatgpt": await self._chatgpt_account()})
            raise AppError(chatgpt_error_text(error)) from error
        account = await self._chatgpt_account()
        self.emit("accounts", {"chatgpt": account})
        self._schedule_models()
        return account

    async def _m_chatgpt_login(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open the browser for "Continue with ChatGPT"; answers when the user has finished there (or it failed or timed out)."""
        return await self._chatgpt_call("login", add=bool(params.get("add")), force_consent=bool(params.get("forceConsent")))

    async def _m_chatgpt_cancelLogin(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._chatgpt_call("cancel_login")

    async def _m_chatgpt_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        client_id = params.get("clientId")
        return await self._chatgpt_call("logout", str(client_id) if client_id else None)

    async def _m_chatgpt_use(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._chatgpt_call("use_account", str(params["clientId"]))

    async def _m_chatgpt_setEnabled(self, params: dict[str, Any]) -> dict[str, Any]:
        self.settings.chatgpt_enabled = bool(params["on"])
        self._save()
        account = await self._chatgpt_account()
        self.emit("accounts", {"chatgpt": account})
        self._schedule_models()
        for folder in self.settings.projects:
            self._spawn(self._reload_project(folder), quiet=True)
        return account

    async def _m_chatgpt_welcomed(self, params: dict[str, Any]) -> dict[str, Any]:
        """The user has seen "You're using your ChatGPT plan": it is not shown again."""
        self.settings.chatgpt_welcomed = True
        self._save()
        account = await self._chatgpt_account()
        self.emit("accounts", {"chatgpt": account})
        return account

    def _providers(self) -> Any:
        if self.providers is None:
            raise AppError("API Provider 功能不可用。")
        return self.providers

    async def _m_providers_save(self, params: dict[str, Any]) -> dict[str, Any]:
        saved = await asyncio.to_thread(self._providers().save, params["draft"])
        self._schedule_models()
        return saved

    async def _m_providers_remove(self, params: dict[str, Any]) -> None:
        provider_id = str(params["id"])
        await asyncio.to_thread(self._providers().remove, provider_id)
        if self.settings.default_model.startswith(f"{API_PREFIX}{provider_id}:"):
            self.settings.default_model = ""  # the model is gone with its provider: defaults() falls back to the first one
            self._save()
        self._schedule_models()

    async def _m_providers_test(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._providers().test(params["draft"])

    async def _m_providers_listModels(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._providers().model_details(params["draft"])

    async def _m_providers_envStatus(self, params: dict[str, Any]) -> dict[str, str]:
        if self.providers is None:
            return {name: "unset" for name in params.get("names", [])}
        return self.providers.env_status(params.get("names", []))

    # ------------------------------------------------------------------ the window and the system

    async def _m_window_minimize(self, params: dict[str, Any]) -> None:
        self.platform.window_minimize()

    async def _m_window_toggleMaximize(self, params: dict[str, Any]) -> bool:
        return self.platform.window_toggle_maximize()

    async def _m_window_close(self, params: dict[str, Any]) -> None:
        self.platform.window_close()

    async def _m_window_startMove(self, params: dict[str, Any]) -> None:
        self.platform.window_start_move()

    async def _m_window_startResize(self, params: dict[str, Any]) -> None:
        self.platform.window_start_resize(str(params.get("edge", "")))

    async def _m_window_isMaximized(self, params: dict[str, Any]) -> bool:
        return self.platform.window_is_maximized()

    async def _m_shell_openExternal(self, params: dict[str, Any]) -> None:
        url = str(params.get("url", ""))
        if url.startswith(("http://", "https://", "mailto:")):
            self.platform.open_url(url)

    # ------------------------------------------------------------------ shutdown

    def busy(self) -> list[Conversation]:
        return [c for c in self.order if c.controller.busy]

    async def shutdown(self) -> None:
        """Close every session (consumers are cancelled first) and stop talking to the page."""
        self._shutting_down = True
        stop_devin = getattr(self.devin, "stop_starting", None)
        if stop_devin is not None:
            stop_devin()
        try:
            await self._stop_consumers()
        finally:
            await self._close_engines()

    async def _stop_consumers(self) -> None:
        try:
            self._write_snapshots_now()
        except Exception:
            log.exception("could not save session snapshots during shutdown")
        self._close_video_preview()
        for flow in list(self.connectors.flows.values()):
            flow.cancel()
        await asyncio.to_thread(self.skill_marketplace.close)
        if self.memory is not None:
            self.memory.flush_runs()
        for handle in (self._flush_handle, self._sidebar_handle, self._memory_handle, self._runs_handle):
            if handle is not None:
                handle.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            _, pending = await asyncio.wait(list(self._tasks), timeout=10)
            if pending:
                log.warning("%d application tasks did not finish during shutdown", len(pending))

    async def _close_engines(self) -> None:
        async def close(engine: Any) -> None:
            try:
                async with asyncio.timeout(30):
                    await engine.close_all()
            except Exception:
                log.exception("%s did not cleanly shut down", type(engine).__name__)

        await asyncio.gather(*(close(engine) for engine in (self.factory, self.devin, self.chatgpt) if engine is not None))


_MISSING = object()


def chatgpt_error_text(error: BaseException) -> str:
    """Like ``error_text``, for what the ChatGPT Plan engine raised."""
    from desktop.core.chatgpt.errors import friendly_failure as chatgpt_failure

    return chatgpt_failure(error)


def devin_error_text(error: BaseException) -> str:
    """Like ``error_text``, for what the Devin engine raised (the JSON-RPC client's errors, worded with Devin's name)."""
    if isinstance(error, (AppError, SessionLimitError)) and error.args:
        return str(error.args[0])
    from desktop.core.devin.errors import friendly_failure as devin_failure

    return devin_failure(error)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def error_text(error: BaseException) -> str:
    if isinstance(error, (AppError, SessionLimitError)) and error.args:
        return str(error.args[0])  # already a sentence for the user
    if type(error).__name__ == "ProviderError":  # core/providers.py: one Chinese sentence as well
        return str(error)
    if isinstance(error, ChatGPTError):
        return chatgpt_error_text(error)
    from desktop.core.engine_errors import EngineError

    if isinstance(error, EngineError):
        return devin_error_text(error)
    return f"{type(error).__name__}: {error}"
