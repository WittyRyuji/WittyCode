"""The Codex engine: the ChatGPT subscription through ``codex app-server``, next to Droid's ``SessionManager``.

One long-lived app-server process serves everything: the account, the model list, the thread list and every open
thread. The protocol allows it (one connection started a dozen threads and ``thread/loaded/list`` listed them; every
turn and item notification seen carried ``threadId``, as do the server's approval requests in the schema), and it is
much lighter than a process per session (the native ``codex.exe`` is 357 MB on disk; Droid needs one per session). The price: if
the process dies, every open Codex session is DEAD at once; each comes back with ``reconnect`` (a new process,
``thread/resume``). The process starts on first use (``connection``) and lives until ``close_all``.

Signing in uses Codex's own ChatGPT browser flow (``account/login/start`` with ``type: "chatgpt"`` -> ``authUrl``,
opened here; the app-server hosts the callback and reports ``account/login/completed``); the tokens stay in Codex's
``~/.codex/auth.json``, shared with the Codex CLI, desktop app and editor extension.

No Qt here: async methods for a qasync loop, listeners like ``SessionManager``'s (``cb(controller, kind)``), plus
``add_account_listener(cb(account_json))`` for login, logout and rate-limit changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desktop import __version__
from desktop.core.codex.account import account_json, is_chatgpt, is_signed_out, merge_rate_limits
from desktop.core.codex.controller import CodexController, CodexUsage, last_call_tokens, saved_turns
from desktop.core.codex.errors import (
    CodexError,
    CodexNotFoundError,
    CodexRpcError,
    CodexSignedOutError,
    friendly_failure,
)
from desktop.core.codex.history import build_turns
from desktop.core.codex.items import split_handoff
from desktop.core.codex.locate import CodexBinary, locate_codex
from desktop.core.codex.models import CodexModel, models_from_result
from desktop.core.codex.policy import DEFAULT_LEVEL, normalize_level
from desktop.core.codex.rollout import read_rollout
from desktop.core.codex.rpc import JsonRpcClient
from desktop.core.handoff import Handoff
from desktop.core.hooks import Hooks
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.session_manager import LiveSettings, Peek, SessionState
from desktop.core.settings import AppSettings, normalize_folder
from desktop.core.toolinfo import first_line
from desktop.core.transcript import Transcript
from desktop.core.usage import Tokens

log = logging.getLogger("desktop.codex")

CLIENT_INFO = {"name": "wittycode", "title": "WittyCode", "version": __version__}
"""``initialize.clientInfo``; Codex records ``name`` as the thread's originator (verified: ``"originator": "wittycode"``)."""
INITIALIZE_SECONDS = 60.0
REQUEST_SECONDS = 30.0
LOGIN_TIMEOUT_SECONDS = 300.0
SESSION_LIST_LIMIT = 50
SOURCE_KINDS = ("cli", "vscode", "appServer")
"""Threads to list: the CLI's, the editor extension's and app-server clients' (WittyCode's own threads are recorded as
``vscode``, verified), not sub-agents or ``codex exec`` runs."""
MODEL_PAGES = 10

Locate = Callable[[str | None], CodexBinary | None]
ClientFactory = Callable[..., JsonRpcClient]


DESKTOP_FLAGS = ("-c", "features.prefer_mxc=true")
"""What the Codex desktop app starts its own app-server with (read off its command line, 2026-10-31). Without
``prefer_mxc`` the sandbox falls back to the legacy Windows one, which on a machine where that was set up long ago
cannot start any command at all in the read-only and workspace levels (``CryptUnprotectData failed``), though the
desktop app itself works there."""


def default_client_factory(binary: CodexBinary, **handlers: Any) -> JsonRpcClient:
    """``codex app-server`` over stdio (the default transport), with the given ``on_*`` handlers.

    The desktop app's own binary gets the flag the desktop app gives it: it is the same program, so it behaves as the
    user's Codex app does. Another CLI (a path in the settings, one on PATH) may not know the flag: no change there.
    """
    flags = DESKTOP_FLAGS if binary.source == "desktop" else ()
    return JsonRpcClient(binary.argv(*flags, "app-server"), **handlers)


@dataclass(frozen=True, slots=True)
class CodexSavedThread:
    """A saved thread for the sidebar, with the attributes the backend reads from Droid's ``SavedSession``."""

    id: str
    title: str
    modified_at: datetime
    created_at: datetime
    message_count: int | None = None
    """Codex's thread list has no message count."""
    cwd: str = ""
    model: str = ""
    owner: str = ""


def _moment(value: Any) -> datetime:
    seconds = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    return datetime.fromtimestamp(seconds, timezone.utc)


def saved_thread(thread: Mapping[str, Any]) -> CodexSavedThread:
    name = str(thread.get("name") or "").strip()
    preview = split_handoff(str(thread.get("preview") or ""))[0]
    return CodexSavedThread(
        id=str(thread.get("id")),
        title=name or first_line(preview, 80),
        modified_at=_moment(thread.get("updatedAt") or thread.get("createdAt")),
        created_at=_moment(thread.get("createdAt")),
        cwd=str(thread.get("cwd") or ""),
        model=str(thread.get("model") or ""),
    )


def cwd_variants(folder: str) -> list[str]:
    """``thread/list`` matches ``cwd`` exactly; editors often record the drive letter in lower case."""
    variants = [folder]
    if len(folder) > 1 and folder[1] == ":":
        for drive in (folder[0].lower(), folder[0].upper()):
            variant = drive + folder[1:]
            if variant not in variants:
                variants.append(variant)
    return variants


class _Login:
    def __init__(self, client: JsonRpcClient, login_id: str) -> None:
        self.client = client
        self.login_id = login_id
        self.done: asyncio.Future[Mapping[str, Any] | None] = asyncio.get_running_loop().create_future()

    def finish(self, outcome: Mapping[str, Any] | None) -> None:
        if not self.done.done():
            self.done.set_result(outcome)


class _Route:
    """Ties the handlers of one client to it, so that a dead client's late messages are told apart."""

    def __init__(self, engine: CodexEngine) -> None:
        self.engine = engine
        self.client: JsonRpcClient | None = None

    def notification(self, method: str, params: Any) -> None:
        self.engine._route_notification(self.client, method, params)

    async def request(self, method: str, params: Any, request_id: Any) -> Any:
        return await self.engine._route_request(self.client, method, params, request_id)

    def exit(self, error: Any) -> None:
        self.engine._lost(self.client, error)


class CodexEngine:
    """Owns the app-server process, the open Codex sessions, the account and the model list."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        locate: Locate | None = None,
        client_factory: ClientFactory | None = None,
        open_url: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        login_timeout: float = LOGIN_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings
        self._locate: Locate = locate if locate is not None else locate_codex
        self._client_factory: ClientFactory = client_factory if client_factory is not None else default_client_factory
        self._open_url = open_url if open_url is not None else webbrowser.open
        self._clock = clock
        self.login_timeout = login_timeout
        self.controllers: list[CodexController] = []
        self.memory: MemoryLaunch | None = None
        """The shared memory, when there is one: every thread started or resumed is given its MCP server."""
        self.hooks = Hooks()
        """The application's hooks (``core/hooks``): consulted before each message of every thread."""
        self._listeners: list[Callable[[CodexController, str], None]] = []
        self._account_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._threads: dict[str, CodexController] = {}
        self._client: JsonRpcClient | None = None
        self._client_lock = asyncio.Lock()
        self._binary: CodexBinary | None = None
        self._binary_for: str | None = None
        self._located = False
        self._models: list[CodexModel] | None = None
        self._models_lock = asyncio.Lock()
        self._windows: dict[str, int] = {}
        self._account: Any = None
        self._rate_limits: Any = None
        self._login: _Login | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.login_url: str | None = None
        """The sign-in page while a sign-in is under way (for a "copy the link" fallback)."""
        self.workspace_write_downgraded = False
        """Codex answered a workspace-write request with read-only (no Windows sandbox set up)."""

    # ------------------------------------------------------------------ listeners

    def add_listener(self, callback: Callable[[CodexController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[CodexController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, controller: CodexController, kind: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(controller, kind)
            except Exception:
                log.exception("codex listener failed")

    def add_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """``callback(account_json)`` after a sign-in, a sign-out or new rate limits."""
        self._account_listeners.append(callback)

    def remove_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._account_listeners:
            self._account_listeners.remove(callback)

    def _emit_account(self, account: dict[str, Any]) -> None:
        for callback in list(self._account_listeners):
            try:
                callback(dict(account))
            except Exception:
                log.exception("codex account listener failed")

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ the process

    async def binary(self, *, refresh: bool = False) -> CodexBinary | None:
        """The Codex CLI in use (located once per ``codex_path`` setting; ``refresh`` looks again)."""
        explicit = (self.settings.codex_path or "").strip() or None
        if refresh or not self._located or self._binary_for != explicit or self._binary is None:
            self._binary = await asyncio.to_thread(self._locate, explicit)
            self._binary_for = explicit
            self._located = True
        return self._binary

    @property
    def running(self) -> bool:
        return self._client is not None and self._client.running

    async def path_changed(self) -> bool:
        """``codex_path`` was changed. What was read from the old program (account, model list, windows) is stale, and
        so is its app-server: it is stopped when no session uses it, so that the next request starts the new program.

        ``False``: sessions are open and keep the old app-server until they are closed (stopping it would end them).
        """
        self._models = None
        self._account = None
        self._rate_limits = None
        self._windows.clear()
        if self.open_controllers():
            return False
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.close()
        return True

    async def connection(self) -> JsonRpcClient:
        """The app-server, started and initialized on first use (and again after it died)."""
        async with self._client_lock:
            client = self._client
            if client is not None and client.running:
                return client
            self._client = None
            binary = await self.binary()
            if binary is None:
                raise CodexNotFoundError()
            route = _Route(self)
            client = self._client_factory(binary, on_notification=route.notification, on_request=route.request, on_exit=route.exit)
            route.client = client
            await client.start()
            try:
                await client.request("initialize", {"clientInfo": dict(CLIENT_INFO), "capabilities": None}, timeout=INITIALIZE_SECONDS)
                await client.notify("initialized")
            except BaseException:
                with contextlib.suppress(Exception):
                    await client.close()
                raise
            log.info("codex app-server %s started (pid %s)", binary.version, client.pid)
            self._client = client
            return client

    def _lost(self, client: JsonRpcClient | None, error: Any) -> None:
        log.warning("codex app-server exited: %s %s", error, getattr(error, "stderr", "")[-500:])
        if self._client is client:
            self._client = None
        for controller in list(self.controllers):
            if controller._client is client:
                controller._connection_lost(error)
        if self._login is not None and self._login.client is client:
            self._login.finish(None)
        if client is not None:
            self._spawn(client.close())  # releases the dead process's pipes and tasks

    def _route_notification(self, client: JsonRpcClient | None, method: str, params: Any) -> None:
        params = params if isinstance(params, Mapping) else {}
        if method == "account/login/completed":
            login = self._login
            if login is not None and params.get("loginId") in (None, login.login_id):
                login.finish(params)
            return
        if method == "account/updated":
            self._spawn(self._refresh_account())
            return
        if method == "account/rateLimits/updated":
            self._rate_limits = merge_rate_limits(self._rate_limits, params.get("rateLimits"))
            self._emit_account(self.cached_account())
            return
        controller = self._threads.get(str(params.get("threadId") or ""))
        if controller is not None and controller._client is client:
            controller._on_notification(method, params)

    async def _route_request(self, client: JsonRpcClient | None, method: str, params: Any, request_id: Any) -> Any:
        params = params if isinstance(params, Mapping) else {}
        controller = self._threads.get(str(params.get("threadId") or ""))
        if controller is not None and controller._client is client:
            return await controller._on_server_request(method, params, request_id)
        if method.endswith("/requestApproval") and "permissions" not in method:
            return {"decision": "cancel"}  # a thread nobody shows (any more)
        raise CodexRpcError(-32601, f"no open WittyCode session for {method}")

    async def windows_sandbox(self) -> str | None:
        """``windowsSandbox/readiness``: ``ready`` | ``notConfigured`` | ``updateRequired`` (``None``: unknown)."""
        try:
            client = await self.connection()
            result = await client.request("windowsSandbox/readiness", None, timeout=REQUEST_SECONDS)
        except CodexError:
            return None
        status = result.get("status") if isinstance(result, Mapping) else None
        return status if isinstance(status, str) else None

    # ------------------------------------------------------------------ the account

    def cached_account(self) -> dict[str, Any]:
        """The account as last read (no request), with the rate limits as last reported."""
        return account_json(self._binary, self._account, self._rate_limits, signing_in=self._login is not None, now=self._clock())

    async def account(self) -> dict[str, Any]:
        """``CodexAccount`` for the page (without ``enabled``)."""
        binary = await self.binary()
        if binary is None:
            return account_json(None)
        try:
            client = await self.connection()
            account = await client.request("account/read", {"refreshToken": False}, timeout=REQUEST_SECONDS)
        except CodexError as error:
            return account_json(binary, None, None, signing_in=self._login is not None, now=self._clock(), error=friendly_failure(error))
        self._account = account
        if is_chatgpt(account):
            try:
                self._rate_limits = await client.request("account/rateLimits/read", None, timeout=REQUEST_SECONDS)
            except CodexError as error:
                log.info("codex rate limits unavailable: %s", error)
        else:
            self._rate_limits = None
        return account_json(binary, account, self._rate_limits, signing_in=self._login is not None, now=self._clock())

    async def _refresh_account(self) -> None:
        with contextlib.suppress(Exception):
            self._emit_account(await self.account())

    async def ensure_signed_in(self, client: JsonRpcClient) -> None:
        """Refuse to start a thread without credentials: its first turn would only fail after a minute of retries."""
        account = await client.request("account/read", {"refreshToken": False}, timeout=REQUEST_SECONDS)
        self._account = account
        if is_signed_out(account):
            raise CodexSignedOutError()

    async def login(self) -> dict[str, Any]:
        """Sign in with ChatGPT in the browser; returns the account once signed in, cancelled or timed out (5 min)."""
        current = self._login
        if current is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(current.done), self.login_timeout)
            return await self.account()
        client = await self.connection()
        result = await client.request("account/login/start", {"type": "chatgpt"}, timeout=REQUEST_SECONDS)
        result = result if isinstance(result, Mapping) else {}
        attempt = _Login(client, str(result.get("loginId") or ""))
        self._login = attempt
        self.login_url = str(result.get("authUrl") or "") or None
        self._emit_account(self.cached_account())
        try:
            if self.login_url:
                try:
                    self._open_url(self.login_url)
                except Exception as error:
                    log.warning("cannot open the sign-in page: %s", error)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(attempt.done), self.login_timeout)
            except asyncio.TimeoutError:
                outcome = None
                await self._cancel(attempt)
            if outcome is not None and not outcome.get("success"):
                log.info("codex sign-in did not complete: %s", outcome.get("error"))
        finally:
            if self._login is attempt:
                self._login = None
                self.login_url = None
        account = await self.account()
        self._emit_account(account)
        return account

    async def _cancel(self, attempt: _Login) -> None:
        if self._login is attempt:
            self._login = None
            self.login_url = None
        attempt.finish(None)
        if attempt.login_id and attempt.client.running:
            with contextlib.suppress(CodexError):
                await attempt.client.request("account/login/cancel", {"loginId": attempt.login_id}, timeout=REQUEST_SECONDS)

    async def cancel_login(self) -> dict[str, Any]:
        attempt = self._login
        if attempt is not None:
            await self._cancel(attempt)
        account = await self.account()
        self._emit_account(account)
        return account

    async def logout(self) -> dict[str, Any]:
        client = await self.connection()
        await client.request("account/logout", None, timeout=REQUEST_SECONDS)
        self._rate_limits = None
        account = await self.account()
        self._emit_account(account)
        return account

    # ------------------------------------------------------------------ models

    async def list_models(self, *, refresh: bool = False) -> list[CodexModel]:
        """The subscription's models (cached; ``refresh`` asks again)."""
        async with self._models_lock:
            if self._models is None or refresh:
                client = await self.connection()
                models: list[CodexModel] = []
                cursor: str | None = None
                for _ in range(MODEL_PAGES):
                    params: dict[str, Any] = {"limit": 100, "includeHidden": False}
                    if cursor:
                        params["cursor"] = cursor
                    result = await client.request("model/list", params, timeout=REQUEST_SECONDS)
                    models.extend(model for model in models_from_result(result) if model.id not in {m.id for m in models})
                    cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
                    if not cursor:
                        break
                self._models = models
            return [self._with_window(model) for model in self._models]

    def open_external(self, url: str) -> None:
        """Open a page in the user's browser (an MCP server asked the user to do something there)."""
        if url.lower().startswith(("http://", "https://")):
            self._open_url(url)

    def memory_params(self, controller: CodexController) -> dict[str, Any]:
        """What ``thread/start`` and ``thread/resume`` get to open the shared memory's MCP server for this thread."""
        command = self.memory.server_for("codex", controller.project, controller.run) if self.memory is not None else None
        if command is None:
            return {}
        server = {"command": command.command, "args": list(command.args), "env": dict(command.env)}
        return {"config": {"mcp_servers": {command.name: server}}}

    def _with_window(self, model: CodexModel) -> CodexModel:
        window = self._windows.get(model.id)
        return replace(model, context_window=window) if window and not model.context_window else model

    def model_info(self, model_id: str) -> CodexModel | None:
        model = next((model for model in self._models or () if model.id == model_id), None)
        return self._with_window(model) if model is not None else None

    def model_window(self, model_id: str) -> int | None:
        if not model_id:
            return None
        info = self.model_info(model_id)
        return self._windows.get(model_id) or (info.context_window if info is not None else None)

    def _learn_window(self, model_id: str, window: int) -> None:
        if model_id and window > 0:
            self._windows[model_id] = window

    # ------------------------------------------------------------------ sessions

    async def list_sessions(self, project: str | Path, limit: int | None = SESSION_LIST_LIMIT) -> list[CodexSavedThread]:
        """Saved threads of ``project``, most recently used first ([] when Codex is not available)."""
        folder = normalize_folder(project)
        params: dict[str, Any] = {"cwd": cwd_variants(folder), "sortKey": "updated_at", "sourceKinds": list(SOURCE_KINDS)}
        if limit:
            params["limit"] = int(limit)
        try:
            client = await self.connection()
            result = await client.request("thread/list", params, timeout=REQUEST_SECONDS)
        except Exception as error:
            log.info("listing codex threads failed: %s", error)
            return []
        data = result.get("data") if isinstance(result, Mapping) else None
        return [saved_thread(thread) for thread in data or () if isinstance(thread, Mapping) and thread.get("id")]

    def find(self, session_id: str) -> CodexController | None:
        return next((c for c in self.controllers if c.session_id == session_id and not c._close_requested), None)

    def open_controllers(self) -> list[CodexController]:
        return [c for c in self.controllers if c.is_open]

    async def open_new(
        self,
        project: str | Path,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        autonomy: str | None = None,
        transcript: Transcript | None = None,
        handoff: Handoff | None = None,
    ) -> CodexController:
        """Start a thread in ``project``. ``transcript`` / ``handoff`` continue a conversation from another engine.

        A failure leaves the controller CLOSED with ``last_error`` (and an error block in the transcript), like Droid's.
        """
        controller = CodexController(
            manager=self,
            project=Path(normalize_folder(project)),
            requested_model=str(model or ""),
            reasoning=str(getattr(reasoning, "value", reasoning) or ""),
            autonomy=normalize_level(autonomy) or normalize_level(self.settings.codex_autonomy) or DEFAULT_LEVEL,
            handoff=handoff,
        )
        if transcript is not None:
            controller.transcript = transcript
        self.controllers.append(controller)
        try:
            await controller.open_new()
        except BaseException as error:
            controller._abandon(error)
            raise
        if controller.state is SessionState.CLOSED:
            self._forget(controller)
        return controller

    async def open_saved(
        self, project: str | Path, thread_id: str, *, title: str | None = None, modified: float | None = None
    ) -> CodexController:
        """Resume a thread. ``modified`` (epoch seconds): when it last had a message, kept as its activity time."""
        existing = self.find(thread_id)
        if existing is not None:
            return existing
        leaving = next((c for c in self.controllers if c.session_id == thread_id and c._close_requested), None)
        if leaving is not None:
            # Its unsubscribe would end this connection's subscription to the thread the new session is about to resume.
            await leaving.close()
        controller = CodexController(
            manager=self,
            project=Path(normalize_folder(project)),
            session_id=thread_id,
            saved_title=title or None,
        )
        if modified is not None:
            controller.active_at = modified
        self.controllers.append(controller)
        try:
            await controller.open_saved()
        except BaseException as error:
            controller._abandon(error)
            raise
        return controller

    async def peek_saved(self, thread_id: str) -> Peek | None:
        """A saved thread read without resuming it: its turns (``thread/turns/list``), its model and reasoning level
        (``thread/read``) and, from the rollout file, its permission level and token figures. ``None``: not readable."""
        try:
            client = await self.connection()
            turns = await saved_turns(client, thread_id)
            read = await client.request("thread/read", {"threadId": thread_id, "includeTurns": False}, timeout=REQUEST_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.info("could not read codex thread %s: %s", thread_id, error)
            return None
        thread = read.get("thread") if isinstance(read, Mapping) else None
        thread = thread if isinstance(thread, Mapping) else {}
        facts = await asyncio.to_thread(read_rollout, thread.get("path"))
        transcript = Transcript()
        if turns:
            transcript.add_history_turns(build_turns(transcript, turns))
        model = str(thread.get("model") or (facts.model if facts else "") or "")
        reasoning = str(thread.get("reasoningEffort") or (facts.effort if facts else "") or "")
        peek = Peek(
            transcript,
            title=str(thread.get("name") or "").strip() or None,
            settings=LiveSettings(model=model, reasoning=reasoning, autonomy=(facts.level if facts else None) or "", mode="auto") if model else None,
        )
        usage = facts.token_usage if facts else None
        if usage is not None:
            total = usage.get("total")
            peek.usage = Tokens.of(CodexUsage.from_breakdown(total)) if isinstance(total, Mapping) else None
            peek.context_used = last_call_tokens(usage.get("last"))
            window = usage.get("modelContextWindow")
            peek.window = int(window) if isinstance(window, (int, float)) and window > 0 else None
            if peek.window:
                self._learn_window(model, peek.window)
        return peek

    async def close(self, controller: CodexController) -> None:
        await controller.close()

    async def close_all(self, timeout: float = 20.0) -> None:
        """Close every session, then the app-server process."""
        controllers = list(self.controllers)
        if controllers:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(c.close() for c in controllers), return_exceptions=True), timeout)
        self.controllers.clear()
        self._threads.clear()
        if self._login is not None:
            self._login.finish(None)
            self._login = None
        if self._tasks:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.close()

    # ------------------------------------------------------------------ bookkeeping (controllers call these)

    def _attach(self, controller: CodexController) -> None:
        if controller.session_id:
            self._threads[controller.session_id] = controller

    def _detach(self, controller: CodexController) -> None:
        for thread_id, owner in list(self._threads.items()):
            if owner is controller:
                del self._threads[thread_id]

    def _forget(self, controller: CodexController) -> None:
        if controller in self.controllers:
            self.controllers.remove(controller)


__all__ = [
    "CLIENT_INFO",
    "CodexEngine",
    "CodexSavedThread",
    "cwd_variants",
    "default_client_factory",
    "saved_thread",
]
