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
from desktop.core.cache import Cache
from desktop.core.catalog import API_PREFIX, Catalog, Target, build_catalog, settings_for_model
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.hooks import Hooks
from desktop.core.llm.backend import is_api_model
from desktop.core import files as filetools
from desktop.core.handoff import AS_TEXT, CarryOption, attachment_text, build_handoff, carry_options, carry_overage, conversation_messages
from desktop.core.harness import goal as goals
from desktop.core import language
from desktop.core.harness import skills
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
    COMPACT_AT_RANGE,
    MAX_OPEN_SESSIONS_RANGE,
    MAX_SESSION_TITLE,
    MEMORY_ENGINES,
    SIDEBAR_WIDTH_RANGE,
    THEMES,
    AppSettings,
    normalize_folder,
    same_folder,
    save_settings,
)
from desktop.core.timefmt import format_relative
from desktop.core.transcript import TurnStatus
from desktop.core.speed import speed_of
from desktop.core.usage import Tokens, UsageLedger
from desktop.core.viewmodel import attachment_json, context_json, turn_json, usage_json

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
"""A running turn moves its session's activity time (sidebar order and age) at most this often, in seconds: output
arrives many times a second, and every move re-sorts the list and patches the row."""
EVICTION_NOTICE = "已自动关闭最久未使用的会话"
RETRY_NOTICE = "这条消息没有发送成功：附件已放回输入框，可以直接重试。"
RETRY_OVER_NOTICE = "这条消息没有发送成功：附件已放回输入框。"
SESSION_LIST_LIMIT = 50
"""How many saved sessions of a project each engine is asked for."""
DEVIN_ROW_PREFIX = "dv:"
"""Row ids of saved Devin sessions in the sidebar."""
CHATGPT_ROW_PREFIX = "cg:"
"""Row ids of saved conversations of WittyCode's own loop (ChatGPT Plan and the API providers) in the sidebar."""
ROW_PREFIXES = {"devin": DEVIN_ROW_PREFIX, "chatgpt": CHATGPT_ROW_PREFIX}
ENGINE_NAMES = {"devin": "Devin", "chatgpt": "ChatGPT Plan"}
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


@dataclass(eq=False)
class Conversation:
    cid: str
    controller: Any
    project: str
    requested: dict[str, str] = field(default_factory=dict)
    """The settings asked for when the session was opened (what the page shows until the engine reports its own)."""
    hidden: set[str] = field(default_factory=set)
    """Native ids of earlier engines' sessions of this conversation: not listed again in the sidebar."""
    sent_ids: list[str] = field(default_factory=list)
    sent_versions: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    drafts: list[Attachment] = field(default_factory=list)
    inflight: tuple[Any, str, tuple[Attachment, ...]] | None = None
    dirty: bool = True
    switching: bool = False
    """A change of engine is under way: the new controller failing to open must not close the conversation."""
    preview: bool = False
    """The page shows the cached conversation while the real history is still being loaded."""
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
    """What the conversation works toward turn after turn (``/goal``, any engine); not kept in a file, a restart ends it."""
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


def _aware(stamp: datetime | None) -> datetime:
    if stamp is None:
        return datetime.fromtimestamp(0, timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def json_copy(value: Any) -> Any:
    """A copy that a thread can write out while the event loop goes on changing the original."""
    return copy.deepcopy(value)


def _row_to_cache(row: SavedRow) -> dict[str, Any]:
    return {"id": row.id, "native": row.native, "engine": row.engine, "title": row.title, "modified": row.modified.timestamp(), "count": row.count, "model": row.model}


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
        self.devin = devin
        self.chatgpt = chatgpt
        self.hooks = Hooks()
        """The application's own hooks (``core/hooks``), one set for every engine."""
        self.memory: MemoryBridge | None = None
        """The shared memory (``core/memory``), when the application has one: every engine's sessions are given it."""
        if memory is not None:
            self.memory = MemoryBridge(memory, row_id=row_id, review_mode=True)
            # the memory's hook is the first
            self.hooks.on_prompt(memory.prompt_hook)
            for engine in (devin, chatgpt):
                if engine is not None and hasattr(engine, "memory"):
                    engine.memory = memory
        # Skills and custom commands (harness/skills): the same hook for every engine, with or without the memory.
        self.hooks.on_prompt(skills.prompt_hook)
        # The language of the models' thinking (core/language): the own loop has it in its instructions.
        self.hooks.on_prompt(language.prompt_hook(lambda: self.settings.think_chinese))
        for engine in (devin, chatgpt):
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
        self._flush_handle: asyncio.TimerHandle | None = None
        self._sidebar_handle: asyncio.TimerHandle | None = None
        self._sidebar_reload: set[str] = set()
        # per attachment object, not per id: every transcript numbers its attachments from the same start, so two
        # conversations have an "a3" each; and an entry goes when its attachment does (a removed draft, a closed tab)
        self._thumbs: weakref.WeakKeyDictionary[Attachment, Thumbnail | None] = weakref.WeakKeyDictionary()
        self._models_loaded = False
        self._shutting_down = False

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
        return DEVIN_PERMISSIONS if engine == "devin" else CHATGPT_PERMISSIONS

    def _autonomy_of(self, engine: str) -> str:
        """The permission level new sessions of an engine start with."""
        s = self.settings
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
            "lastProject": self.settings.last_project,
            "compactAuto": self.settings.chatgpt_compact,
            "compactAt": self.settings.chatgpt_compact_at,
            "thinkChinese": self.settings.think_chinese,
        }

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

    def _rows_for(self, folder: str) -> list[dict[str, Any]]:
        open_here = [c for c in self.order if same_folder(c.project, folder)]
        taken: set[str] = set()
        for conv in open_here:
            taken |= conv.hidden
            native = getattr(conv.controller, "session_id", None)
            if native:
                taken.add(native)
                taken.update(prefix + native for prefix in ROW_PREFIXES.values())
        rows: list[tuple[datetime, dict[str, Any]]] = []
        for saved in self._saved.get(normalize_folder(folder), []):
            if saved.native in taken or saved.id in taken or saved.id in self.settings.archived_sessions:
                continue
            rows.append((saved.modified, self._saved_json(saved, folder)))
        for conv in open_here:
            stamp = datetime.fromtimestamp(getattr(conv.controller, "active_at", time.time()), timezone.utc)
            rows.append((stamp, {"id": conv.cid}))
        rows.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in rows]

    def _saved_json(self, saved: SavedRow, folder: str) -> dict[str, Any]:
        return {
            "id": saved.id,
            "projectId": folder,
            "title": self.settings.session_titles.get(saved.id) or saved.title or "（无标题）",
            "modified": format_relative(saved.modified),
            "modifiedAt": int(saved.modified.timestamp() * 1000),
            "engine": saved.engine,
            "settings": {"model": saved.model, "reasoning": "", "autonomy": "", "mode": "auto"},
            "usage": None,
            "context": {"used": None, "window": 0, "model": "", "compacted": False},
            "workingLabel": "",
        }

    def _saved_sessions_json(self) -> dict[str, Any]:
        sessions = {}
        for folder in self.settings.projects:
            for saved in self._saved.get(normalize_folder(folder), []):
                if saved.id not in self.settings.archived_sessions:
                    sessions[saved.id] = self._saved_json(saved, folder)
        return sessions

    def _archived_json(self) -> list[dict[str, Any]]:
        """The archived conversations the engines still list, the one archived last first."""
        found: list[tuple[int, dict[str, Any]]] = []
        for folder in self.settings.projects:
            for saved in self._saved.get(normalize_folder(folder), []):
                stamp = self.settings.archived_sessions.get(saved.id)
                if stamp is not None:
                    found.append((stamp, self._saved_json(saved, folder)))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in found]

    def _emit_sidebar(self) -> None:
        self.emit("sidebar", {"projects": self.project_rows(), "sessions": self._saved_sessions_json(), "archived": self._archived_json()})

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
        if self.devin is not None and self.settings.devin_enabled:
            with contextlib.suppress(Exception):
                for item in await self.devin.list_sessions(folder, SESSION_LIST_LIMIT):
                    devin_row = DEVIN_ROW_PREFIX + item.id
                    rows.append(SavedRow(devin_row, item.id, "devin", item.title or "", self._latest(devin_row, item.modified_at), item.message_count or 0))
        if self.chatgpt is not None and (self.settings.chatgpt_enabled or self._has_api_models()):
            with contextlib.suppress(Exception):
                for item in await self.chatgpt.list_sessions(folder, SESSION_LIST_LIMIT):
                    chatgpt_row = CHATGPT_ROW_PREFIX + item.id
                    stamp = datetime.fromtimestamp(item.modified_at, timezone.utc)
                    page_model = self.catalog.page_id("chatgpt", item.model) if item.model and self.catalog is not None else ""
                    rows.append(SavedRow(chatgpt_row, item.id, "chatgpt", item.title or "", self._latest(chatgpt_row, stamp), item.messages, page_model))
        self._saved[normalize_folder(folder)] = rows
        self._remember_rows(folder, rows)
        self._schedule_sidebar(folder)

    # ------------------------------------------------------------------ models and accounts

    async def _load_models(self) -> None:
        folder = self.settings.projects[0] if self.settings.projects else str(Path.home())

        async def from_devin() -> list[Any]:
            if self.devin is None or not self.settings.devin_enabled:
                return []
            with contextlib.suppress(Exception):
                account = await self.devin.account()
                if account.get("auth") == "signed-in":
                    return list(await self.devin.list_models())
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
        devin_models, chatgpt_models, api_models = await asyncio.gather(from_devin(), from_chatgpt(), from_providers())
        provider_of = getattr(self.providers, "provider_of", None)
        window_of = getattr(self.providers, "window_of", None)
        self.catalog = build_catalog(
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

    def _schedule_models(self) -> None:
        self._spawn(self._load_models(), quiet=True)

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

    async def _refresh_accounts(self) -> None:
        """The slow part of the accounts (``devin auth status``) once the page is up."""
        devin = await self._devin_account()
        # Read when it is sent, not while the slow one was awaited: the user may have dismissed the one-time note or
        # signed in meanwhile, and a late event with the old values would bring it all back.
        self.emit("accounts", {"devin": devin, "chatgpt": self._chatgpt_now()})

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
        turns = controller.transcript.turns
        if not native or not turns:
            return None
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
            controller.title,
            controller.active_at,
            listed,
            {name: meta[name] for name in ("settings", "usage", "speed", "context")},
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
        if conv.cache_handle is None and not self._shutting_down and conv.controller.state in (SessionState.READY, SessionState.DEAD):
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
            if conv.cache_dirty and conv.controller.state is not SessionState.RUNNING:
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
        conv.cached_meta = dict(snapshot.get("meta") or {})
        conv.sent_ids = ["preview"]  # whatever is sent next replaces the list instead of adding to it
        patch = {
            "id": conv.cid,
            "projectId": conv.project,
            "mark": self._mark(conv),
            "engine": conv.engine,
            "title": snapshot.get("title") or "",
            **conv.cached_meta,
        }
        self.emit("session", {"id": conv.cid, "patch": patch})
        self.emit("turns", {"id": conv.cid, "turns": list(snapshot["turns"]), "all": True})

    # ------------------------------------------------------------------ init

    async def _m_app_init(self, params: dict[str, Any]) -> dict[str, Any]:
        self._hydrate()
        for conv in self.order:  # a page that has just (re)started knows nothing of the open conversations yet
            conv.meta, conv.sent_ids, conv.sent_versions, conv.dirty = {}, [], {}, True
        self._schedule_flush()
        for folder in list(self.settings.projects):
            self._spawn(self._reload_project(folder), quiet=True)
        self._schedule_models()
        self._spawn(self._refresh_accounts(), quiet=True)
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
        conv.dirty = True
        if kind == "state":
            self._schedule_sidebar(conv.project, reload=controller.state is SessionState.READY)
        elif controller.state is SessionState.RUNNING and time.time() - controller.active_at >= ACTIVITY_STEP:
            # A long turn is conversation too: its time follows the latest output, or a session still working for ten
            # minutes would sink below one that said a word meanwhile.
            controller.active_at = time.time()
            self._schedule_sidebar(conv.project)
        if kind == "steer":
            self._return_steers(conv)
        if controller.state is SessionState.RUNNING:
            conv.ran = True
        elif conv.queue or conv.ran:
            asyncio.get_running_loop().call_soon(self._follow_queue, conv)
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
        )
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
        if native and active:
            row = row_id(conv.engine, native)
            self._spoken[row] = max(self._spoken.get(row, 0.0), active)

    def _remove(self, conv: Conversation) -> None:
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
        """The four settings under the message box: the session's own once it is connected; until then what it was left
        with (read from the engine's file, else the cache) with whatever the user has chosen since laid over it."""
        live = conv.controller.live_settings() if hasattr(conv.controller, "live_settings") else None
        if live is not None and conv.controller.state in (SessionState.READY, SessionState.RUNNING):
            return self._settings_of(conv.engine, live)
        saved = getattr(conv.controller, "saved_settings", None)
        left = self._settings_of(conv.engine, saved) if saved is not None else dict(conv.cached_meta.get("settings") or {})
        return {**left, **conv.requested}

    def _mark(self, conv: Conversation) -> str:
        controller = conv.controller
        state = controller.state
        if state is SessionState.RUNNING:
            asking = controller.transcript.pending_prompts() or getattr(controller, "agent_prompts", lambda: [])()
            return "attention" if asking else "running"
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

    def _row_of(self, conv: Conversation) -> str:
        """The sidebar row of an open conversation ("" until the engine has given it an id of its own)."""
        native = getattr(conv.controller, "session_id", None)
        return row_id(conv.engine, native) if native else ""

    def _title_of(self, conv: Conversation) -> str:
        """The name the user gave the conversation here, else the engine's title."""
        return self.settings.session_titles.get(self._row_of(conv)) or conv.controller.title

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
        active = datetime.fromtimestamp(controller.active_at, timezone.utc)
        self._feed_usage(conv)
        # What the running turn has cost so far but the engine has not booked yet (Devin gives a prompt's figure only when
        # it ends): shown with the total, so the number moves while the turn runs, but never fed to the ledger.
        total = conv.ledger.total
        pending_of = getattr(controller, "usage_pending", None)
        pending = pending_of() if pending_of is not None else None
        if pending is not None:
            total = pending if total is None else total.plus(pending)
        # Until the live session reports its own, the page keeps what was known when it was last cached.
        usage = usage_json(total) or cached.get("usage")
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
            "workingLabel": controller.working_label,
            "modified": format_relative(active),
            "modifiedAt": int(controller.active_at * 1000),
            "engine": conv.engine,
            "nativeId": controller.session_id or "",
            "settings": settings,
            "usage": usage,
            "speed": speed,
            "context": context,
            "queued": self._queued_json(conv),
            "agents": self._agents_json(conv),
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
        # The cached conversation stays up until the real history has arrived (or the session is open without any).
        hold = conv.preview and not turns and controller.state is SessionState.OPENING
        if conv.preview and not hold:
            conv.preview = False
        ids = [turn.id for turn in turns]
        replace = ids[: len(conv.sent_ids)] != conv.sent_ids
        if replace:
            conv.sent_versions = {}
        changed = [t for t in turns if conv.sent_versions.get(t.id) != t.version]
        if (changed or replace) and not hold:
            built = []
            for turn in changed:
                entry = (turn.version, turn_json(turn, self._thumb))
                conv.json_cache[turn.id] = entry
                built.append(entry[1])
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
        if turn.status is not TurnStatus.FAILED:
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
            start = self.settings.projects[-1] if self.settings.projects else str(Path.home())
            folder = await self.platform.pick_folder(start)
            if not folder:
                return {"added": False}
        if not Path(folder).is_dir():
            raise AppError(f"不是有效的文件夹：{folder}")
        if not self.settings.add_project(folder):
            raise AppError("这个文件夹已经在项目列表里了。")
        self._save()
        added = self.settings.projects[-1]
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
        page_model = params.get("modelId") or self.defaults()["model"]
        settings = self._open_settings(page_model)
        await self._make_room()
        self._select_next = True
        self._opening_meta = settings
        try:
            controller = await self._open_controller(folder, page_model, settings)
        finally:
            self._opening_meta = None
            self._select_next = False
        conv = self._conv_of(controller)
        if controller.state is SessionState.CLOSED:
            raise AppError(controller.last_error or "会话没有启动。")
        return {"id": conv.cid if conv else None}

    async def _m_session_openSaved(self, params: dict[str, Any]) -> dict[str, Any]:
        row_id: str = params["id"]
        folder = normalize_folder(params["projectId"])
        for conv in self.order:
            native = getattr(conv.controller, "session_id", None)
            if row_id in (conv.cid, native, *(prefix + (native or "") for prefix in ROW_PREFIXES.values())) or row_id in conv.hidden:
                return {"id": conv.cid}
        # Opening is not a message: the session keeps the time of its last one, so it stays where it was in the list.
        saved = next((item for item in self._saved.get(folder, []) if item.id == row_id), None)
        modified = saved.modified.timestamp() if saved is not None else None
        seed, preview = await self._cached_conversation(row_id, modified)
        await self._make_room()
        self._select_next = True
        self._opening_meta = {}
        self._preview = preview
        self._ledger_seed = seed
        try:
            if row_id.startswith(DEVIN_ROW_PREFIX):
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
        conv = self._conv_of(controller)
        return {"id": conv.cid if conv else None}

    async def _cached_conversation(
        self, row_id: str, modified: float | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The cached copy of a saved session: ``(the copy, the copy if it is also fit to be shown)``.

        It is shown only when it is as new as the session itself (one that was used elsewhere since would be replaced a
        moment later by a very different one). Its token ledger is used whatever its age: the engine's figure includes
        what happened elsewhere, and only the part after the ledger's last figure is added."""
        if not self.cache.enabled:
            return None, None
        snapshot = await asyncio.to_thread(self.cache.load_session, row_id)
        if not snapshot:
            return None, None
        if not snapshot.get("turns"):
            return snapshot, None
        return snapshot, (snapshot if _is_fresh(snapshot, modified) else None)

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
        known = any(item.id == ident for rows in self._saved.values() for item in rows)
        if not known and ident not in self.settings.archived_sessions:
            raise AppError("找不到这个会话。")
        return ident, None

    def _has_content(self, conv: Conversation, row: str) -> bool:
        """Something was said in it, or it is one of the engine's saved sessions: there is something to archive or name."""
        if not row:
            return False
        return bool(conv.controller.transcript.turns) or any(item.id == row for rows in self._saved.values() for item in rows)

    async def _m_session_archive(self, params: dict[str, Any]) -> dict[str, Any]:
        row, conv = self._row_for(params.get("id"))
        if conv is not None:
            if conv.controller.busy:
                raise AppError("这个会话还在运行：先停止它，再归档。")
            if not self._has_content(conv, row):
                raise AppError("这个会话还没有内容，直接关闭就行。")
            await self._close_conversation(conv)
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
        if conv is not None and not self._has_content(conv, row):
            raise AppError("这个会话还没有内容，发出第一条消息之后才能改名。")
        name = " ".join(title.split())[:MAX_SESSION_TITLE]
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
        if conv.controller.busy:
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
            if conv.goal is None:
                return {"ok": True, "text": "现在没有进行中的目标。"}
            conv.goal = None
            conv.dirty = True  # (the page shows no goal any more: closing the window need not warn about it)
            self._schedule_flush()
            return {"ok": True, "text": "目标已取消。"}
        controller = conv.controller
        if not text:
            return {"ok": False, "text": "用法：/goal 目标，例如 /goal 让所有测试通过。"}
        if controller.state is not SessionState.READY:
            return {"ok": False, "text": "会话还没就绪，或这一轮还在运行：结束后再设目标。"}
        if conv.queue:
            return {"ok": False, "text": "还有排队的消息没有发出：它们发完后再设目标。"}
        if self._page_settings(conv).get("mode") == "spec":
            return {"ok": False, "text": "Plan 模式只出方案、不动手，做不了目标：先用 /plan 回到 Auto。"}
        conv.goal = goals.Goal(text)
        if not controller.send_prompt(goals.opening(text)):
            conv.goal = None
            return {"ok": False, "text": "现在不能发送：会话没有就绪。"}
        controller.transcript.add_notice(
            f"目标已设定：{text}\n模型会一轮轮做下去，每轮都把目标交给它对照结果判断，最多 {goals.ROUNDS} 轮；完成或需要你时会停下，点「停止」随时结束。"
        )
        conv.dirty = True
        self._schedule_flush()
        return {"ok": True, "text": ""}

    def _stop_goal(self, conv: Conversation, notice: str) -> None:
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
        if turn is None or turn.status is not TurnStatus.DONE:
            why = "这一轮出错了。" if turn is not None and turn.status is TurnStatus.FAILED else "这一轮被停止了。"
            self._stop_goal(conv, "目标先停下了：" + why)
            return
        texts = [block.text for block in turn.blocks if block.kind == "text" and block.text]
        outcome = goals.step(goal, texts[-1] if texts else "", bool(turn.tools))
        if not outcome.prompt:
            self._stop_goal(conv, outcome.notice)
        elif controller.send_prompt(outcome.prompt):
            conv.dirty = True
            self._schedule_flush()
        else:
            self._stop_goal(conv, "目标先停下了：下一轮没能发出（会话不能接收消息）。")

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

    async def _m_session_send(self, params: dict[str, Any]) -> dict[str, Any]:
        conv = self._conversation(params["id"])
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
        text = str(params.get("text", "")).strip()
        how = params.get("how")
        if how is not None and how not in FOLLOW_UPS:
            raise AppError("发送方式不合法。")
        state = controller.state

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
            conv.queue.append(QueuedMessage(f"q{next(self._queue_ids)}", text, tuple(attachments)))
            taken()
            conv.dirty = True
            self._schedule_flush()
            if state is SessionState.OPENING:
                if conv.queue_task is None or conv.queue_task.done():
                    conv.queue_task = self._spawn(self._send_when_ready(conv), quiet=True)
            elif state is SessionState.READY:
                self._pump(conv)
            return {"ok": True, "queued": True}
        if not controller.send_prompt(text, attachments):
            raise AppError("现在不能发送：会话没有就绪。")
        taken()
        if attachments:
            conv.inflight = (controller.transcript.turns[-1], text, tuple(attachments))
        return {"ok": True}

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
            out.append(
                {
                    "id": agent.session_id,
                    "name": agent.name,
                    "role": agent.role,
                    "model": (model or {}).get("label") or agent.agent_model,
                    "effort": agent.agent_effort,
                    "task": agent.task,
                    "status": agent.status,
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
        if not conv.queue or controller.state is not SessionState.READY or conv.cid not in self._by_cid:
            return
        head = conv.queue[0]
        if not controller.send_prompt(head.text, head.attachments):
            return
        conv.queue.pop(0)
        if head.attachments:
            conv.inflight = (controller.transcript.turns[-1], head.text, head.attachments)
        conv.dirty = True
        self._schedule_flush()

    def _follow_queue(self, conv: Conversation) -> None:
        """The state of the conversation changed with messages waiting (or a turn that has just ended)."""
        if conv.cid not in self._by_cid:
            return
        controller = conv.controller
        state = controller.state
        if state is SessionState.RUNNING:
            conv.ran = True
        elif state is SessionState.READY:
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
        text = text.strip()
        if not text and not item.attachments:
            raise AppError("消息不能是空的：不想发这条，请点取消。")
        conv.queue[index] = dataclasses.replace(item, text=text)
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

    def _file_named(self, params: dict[str, Any]) -> tuple[Path, filetools.Ref]:
        conv = self._conversation(params["id"])
        raw = params.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise AppError("没有指定文件。")
        try:
            return filetools.resolve(raw, conv.project)
        except filetools.FileProblem as problem:
            raise AppError(str(problem)) from problem

    async def _m_files_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Where a path the way the model wrote it is, for the page's menu (copy the path)."""
        path, ref = self._file_named(params)
        return {"path": str(path), "isDir": path.is_dir(), "line": ref.line}

    async def _m_files_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """A file for the side panel: its text, or for a PDF the size of every page and for a picture just its size (the page
        asks for what it draws with ``files.page``). The project's folder is where a relative path is looked for."""
        path, ref = self._file_named(params)
        kind = filetools.preview_kind(path)
        if kind is not None:
            try:
                size = filetools.preview_size(path, kind)
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
        """What the side panel draws of a PDF or a picture: one page of the PDF at the width asked for, or the picture itself
        (``data:`` URIs)."""
        path, _ = self._file_named(params)
        kind = filetools.preview_kind(path)
        page, width = params.get("page", 0), params.get("width", 900)
        if kind is None:
            raise AppError("这个文件不能在侧栏里画出来。")
        if not (isinstance(page, int) and not isinstance(page, bool) and isinstance(width, int) and not isinstance(width, bool)) or page < 0:
            raise AppError("页码或宽度不合法。")
        try:
            url = filetools.image_data_url(path) if kind == "image" else self.platform.pdf_page(str(path), page, width)
        except filetools.FileProblem as problem:
            raise AppError(str(problem)) from problem
        return {"url": url}

    async def _m_files_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open a file outside the window: with the default program, by the system's "open with" choice, or selected in its
        folder. A program or a script is never run by a click: the default way shows it in its folder instead."""
        how = params.get("how")
        if how not in ("default", "choose", "reveal"):
            raise AppError("打开方式不合法。")
        path, _ = self._file_named(params)
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
            self._set_default(field_name, value)
            return {"ok": True}
        conv = self._conversation(cid)
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
            return await self._choose_model(conv, value, carry_ids, over_ok is True)
        ok = await self._update_live(conv, **{field_name: value})
        if ok:
            self._set_default(field_name, value, engine=conv.engine)
        return {"ok": ok}

    def _set_default(self, field_name: str, value: str, *, engine: str | None = None) -> None:
        settings = self.settings
        if field_name == "model":
            settings.default_model = value
            # The level and permission that go with the new model's engine.
            fixed, _ = settings_for_model(self.catalog, self._engine_defaults(value), value)
            settings.reasoning_effort = fixed.get("reasoning", "")
        elif field_name == "reasoning":
            settings.reasoning_effort = value
        elif field_name == "autonomy":
            current = engine or self.catalog.engine_of(settings.default_model)
            if current == "devin" and value in DEVIN_PERMISSIONS:
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

    async def _choose_model(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None = None, over_ok: bool = False
    ) -> dict[str, Any]:
        target = self.catalog.target(page_model)
        if target.engine != conv.engine:
            return await self._switch_engine(conv, page_model, carry_ids, over_ok)
        current = self._page_settings(conv)
        settings, changed = settings_for_model(self.catalog, current, page_model)
        changes: dict[str, str] = {"model": target.model}
        if "reasoning" in changed and settings.get("reasoning"):
            changes["reasoning"] = settings["reasoning"]
        ok = await self._update_live(conv, **changes)
        if ok:
            self._set_default("model", page_model)
            if "reasoning" in changed:
                model = self.catalog.find(page_model) or {}
                self.say(
                    f"{model.get('label', page_model)} 没有推理档位。"
                    if not settings.get("reasoning")
                    else f"{model.get('label', page_model)} 不支持「{current.get('reasoning', '')}」推理，已改为「{settings['reasoning']}」。"
                )
        return {"ok": ok}

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
        if info is not None and not info.supports_images:
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

    async def _switch_engine(
        self, conv: Conversation, page_model: str, carry_ids: Sequence[str] | None = None, over_ok: bool = False
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
        if old.busy:
            raise AppError("本轮还在运行：结束后才能换引擎。")
        if old.state is not SessionState.READY:
            raise AppError("会话还没有就绪，稍后再换引擎。")
        model = self.catalog.find(page_model) or {"label": page_model, "window": 0}
        was = self.catalog.find(self._page_settings(conv).get("model", "")) or {"label": old.model}
        transcript = old.transcript
        options = carry_options(
            transcript.turns, images_problem=self._images_problem(page_model), scans_problem=self._scans_problem(page_model)
        )
        # what the composer already holds goes with the next message too: it takes its share of the limits
        in_box, in_box_bytes = len(conv.drafts), sum(item.size or 0 for item in conv.drafts)
        if options and carry_ids is None:
            return {
                "ok": False,
                "choose": {
                    "attachments": [self._carry_json(option) for option in options],
                    "limits": {"files": SOFT_ATTACHMENTS, "bytes": SOFT_TOTAL_BYTES},
                    "hard": {"files": HARD_ATTACHMENTS, "bytes": HARD_TOTAL_BYTES},
                    "drafts": {"count": in_box, "bytes": in_box_bytes},
                },
            }
        chosen = self._resolve_carry(options, carry_ids or ())
        held = carry_overage(chosen, other_count=in_box, other_bytes=in_box_bytes)
        if held.beyond_ceiling:
            raise AppError(too_many(held))
        if held.over and not over_ok:
            raise AppError(f"{held.describe()}：要这样带过去，请在清单里确认。")
        # What goes along depends on the new model's window. The model list usually knows it; where it does not, it is
        # only reported once the session is open, so the session opens with a hand-over for the figure known now
        # (nothing may be sent without one) and gets the final one when the window arrives.
        window = int(model.get("window") or 0)
        sent_as_files = any(not attachment_text(item).strip() for item in chosen)
        handoff = (
            build_handoff(transcript.turns, window_tokens=window, carry=chosen)
            if sent_as_files or conversation_messages(transcript.turns, carry={item.id for item in chosen})
            else None
        )
        settings = self._open_settings(page_model, {**self._page_settings(conv), "autonomy": ""})
        target = self.catalog.target(page_model)
        previous_requested = conv.requested
        before = transcript.snapshot()
        # What the old session counted is final: the total keeps it, and the new session's figures (it counts from zero)
        # are new ones. A figure the shared transcript keeps goes too, or the new session would start from the one an
        # earlier session left there.
        self._feed_usage(conv)
        ledger_before = (conv.ledger.total, dict(conv.ledger.seen))
        usage_before = transcript.usage
        conv.ledger.restart()
        transcript.usage = None
        conv.requested = dict(settings)
        conv.switching = True
        self._adopt = conv
        controller: Any = None
        try:
            controller = await self._open_controller(
                conv.project, page_model, settings, transcript=transcript, handoff=handoff, replacing=True
            )
            if controller.state is SessionState.CLOSED:
                raise AppError(controller.last_error or "没有切换成功：新的会话没有启动。")
        except BaseException as error:
            self._rollback_switch(conv, old, controller)
            transcript.restore(before)  # a failed open writes its error into the shared transcript
            conv.ledger.total, conv.ledger.seen = ledger_before
            transcript.usage = usage_before
            conv.requested = previous_requested
            if isinstance(error, (AppError, asyncio.CancelledError)):
                raise
            raise AppError(f"没有切换成功：{error_text(error)}") from error
        finally:
            conv.switching = False
            self._adopt = None
        if handoff is not None and not window:
            window = await self._window_of(controller, page_model)
            if window and getattr(controller, "handoff", None) is handoff and controller.state is SessionState.READY:
                handoff = controller.handoff = build_handoff(transcript.turns, window_tokens=window, carry=chosen)
        if old.session_id:
            conv.hidden.add(old.session_id)
        transcript.model = target.model
        if handoff is not None and handoff.carried:
            transcript.add_switch(
                from_model=was.get("label", ""),
                from_engine=engine_label(old.engine, was),
                to_model=model["label"],
                to_engine=engine_label(target.engine, model),
                carried=handoff.carried,
                total=handoff.total,
                size=handoff.size,
                truncated=handoff.truncated,
            )
        self._spawn(old.close(), quiet=True)
        self._set_default("model", page_model)
        self._emit_sidebar()
        conv.meta = {}
        conv.dirty = True
        self._flush(conv)
        note = f"已换到 {model['label']}（{engine_label(target.engine, model)}）。"
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
        for folder in projects:
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
        elif key == "thinkChinese" and isinstance(value, bool):
            s.think_chinese = value
        elif key == "maxOpen" and _is_int(value) and MAX_OPEN_SESSIONS_RANGE[0] <= value <= MAX_OPEN_SESSIONS_RANGE[1]:
            s.max_open_sessions = value
        elif key == "sidebarWidth" and _is_int(value) and SIDEBAR_WIDTH_RANGE[0] <= value <= SIDEBAR_WIDTH_RANGE[1]:
            s.sidebar_width = value
        elif key == "devinPath" and isinstance(value, str):
            changed = value.strip() != s.devin_path
            s.devin_path = value.strip()
            if changed and self.devin is not None:
                self._spawn(self._devin_path_changed(), quiet=True)
        else:
            raise AppError("这个设置的值不合法。")
        self._save()
        view = self.settings_view()
        self.emit("settings", {"settings": view})
        return view

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
        self._write_snapshots_now()
        self._shutting_down = True
        if self.memory is not None:
            self.memory.flush_runs()
        for handle in (self._flush_handle, self._sidebar_handle, self._memory_handle, self._runs_handle):
            if handle is not None:
                handle.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self.devin is not None:
            await self.devin.close_all()
        if self.chatgpt is not None:
            await self.chatgpt.close_all()


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
