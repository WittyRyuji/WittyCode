"""The optional Devin engine: the user's Devin account (Cognition's agent) through ``devin acp``, next to WittyCode's own
loop (``core/chatgpt``).

One long-lived ``devin acp`` process serves everything: the model list, the session list and every open session (every
notification and request carries ``sessionId``; two sessions at once on one connection were exercised on the real
program). The price: if the process dies, every open Devin session is DEAD at once; each comes back with ``reconnect`` (a
new process, ``session/load``). The process starts on first use (``connection``) and lives until ``close_all``.

The account is the CLI's own: ``devin auth status`` says whether it is signed in (and as whom), ``devin auth login`` is its
browser sign-in, ``devin auth logout`` ends it. The tokens stay in Devin's ``credentials.toml``; the same sign-in serves the
Devin desktop app and the CLI.

No Qt here: async methods for a qasync loop, listeners like the other engines' (``cb(controller, kind)``), plus
``add_account_listener(cb(account_json))`` for sign-in and sign-out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desktop import __version__
from desktop.core.jsonrpc import CREATE_NO_WINDOW
from desktop.core.devin import items
from desktop.core.devin.account import DevinStatus, account_json, parse_status
from desktop.core.devin.controller import DevinController
from desktop.core.devin.errors import (
    DevinError,
    DevinNotFoundError,
    DevinProcessError,
    DevinRpcError,
    DevinSignedOutError,
    friendly_failure,
)
from desktop.core.devin.locate import DevinBinary, locate_devin
from desktop.core.devin.models import DevinModel, build_models, cli_info, model_entries, thought_levels
from desktop.core.devin.policy import CANCELLED, DEFAULT_LEVEL, normalize_level
from desktop.core.devin.rpc import AcpClient
from desktop.core.handoff import Handoff
from desktop.core.hooks import Hooks
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.session_state import SessionState
from desktop.core.settings import AppSettings, normalize_folder
from desktop.core.transcript import Transcript

log = logging.getLogger("desktop.devin")

CLIENT_INFO = {"name": "wittycode", "title": "WittyCode", "version": __version__}
INITIALIZE_SECONDS = 60.0
REQUEST_SECONDS = 30.0
NEW_SESSION_SECONDS = 120.0
STATUS_SECONDS = 60.0
LOGIN_TIMEOUT_SECONDS = 300.0
SESSION_LIST_LIMIT = 50
SIGNED_IN_CACHE_SECONDS = 300.0
"""A sign-in seen this recently is not asked about again before a session is opened (the check takes about three seconds)."""

Locate = Callable[[str | None], DevinBinary | None]
ClientFactory = Callable[..., AcpClient]


def default_client_factory(binary: DevinBinary, **handlers: Any) -> AcpClient:
    """``devin acp`` over stdio, with the given ``on_*`` handlers."""
    return AcpClient(binary.argv("acp"), **handlers)


@dataclass(frozen=True, slots=True)
class DevinSavedSession:
    """A saved session for the sidebar, with the attributes the backend reads of every engine's saved sessions."""

    id: str
    title: str
    modified_at: datetime
    created_at: datetime
    message_count: int | None = None
    """Devin's session list has no message count."""
    cwd: str = ""
    model: str = ""
    owner: str = ""


def _moment(value: Any) -> datetime:
    seconds = items.parse_time(value)
    return datetime.fromtimestamp(seconds or 0.0, timezone.utc)


def saved_session(raw: Mapping[str, Any]) -> DevinSavedSession:
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), Mapping) else {}
    updated = raw.get("updatedAt") or meta.get("cognition.ai/createdAt")
    return DevinSavedSession(
        id=str(raw.get("sessionId")),
        title=str(raw.get("title") or "").strip(),
        modified_at=_moment(updated),
        created_at=_moment(meta.get("cognition.ai/createdAt") or updated),
        cwd=str(raw.get("cwd") or ""),
    )


def cwd_variants(folder: str) -> list[str]:
    """Devin matches ``cwd`` as it was given when the session was made; editors often record the drive letter in lower case."""
    variants = [folder]
    if len(folder) > 1 and folder[1] == ":":
        for drive in (folder[0].lower(), folder[0].upper()):
            variant = drive + folder[1:]
            if variant not in variants:
                variants.append(variant)
    return variants


class _Login:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.done: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()

    def finish(self, code: int | None) -> None:
        if not self.done.done():
            self.done.set_result(code)


class _Route:
    """Ties the handlers of one client to it, so that a dead client's late messages are told apart."""

    def __init__(self, engine: DevinEngine) -> None:
        self.engine = engine
        self.client: AcpClient | None = None

    def notification(self, method: str, params: Any) -> None:
        self.engine._route_notification(self.client, method, params)

    async def request(self, method: str, params: Any, request_id: Any) -> Any:
        return await self.engine._route_request(self.client, method, params, request_id)

    def exit(self, error: Any) -> None:
        self.engine._lost(self.client, error)


class DevinEngine:
    """Owns the ``devin acp`` process, the open Devin sessions, the account and the model list."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        locate: Locate | None = None,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.time,
        login_timeout: float = LOGIN_TIMEOUT_SECONDS,
        cli_env: Mapping[str, str] | None = None,
        open_url: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._locate: Locate = locate if locate is not None else locate_devin
        self._client_factory: ClientFactory = client_factory if client_factory is not None else default_client_factory
        self._open_url = open_url if open_url is not None else webbrowser.open
        self._clock = clock
        self.login_timeout = login_timeout
        self._cli_env = dict(cli_env) if cli_env is not None else None
        self.controllers: list[DevinController] = []
        self.memory: MemoryLaunch | None = None
        """The shared memory, when there is one: every session opened is given its MCP server."""
        self.hooks = Hooks()
        """The application's hooks (``core/hooks``): consulted before each message of every session."""
        self._listeners: list[Callable[[DevinController, str], None]] = []
        self._account_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._sessions: dict[str, DevinController] = {}
        self._client: AcpClient | None = None
        self._retired: list[AcpClient] = []
        """Processes that were let go (the path changed, they died): ``close_all`` sees to it that each is really gone."""
        self._client_lock = asyncio.Lock()
        self._binary: DevinBinary | None = None
        self._binary_for: str | None = None
        self._located = False
        self._models: list[DevinModel] | None = None
        self._models_lock = asyncio.Lock()
        self._windows: dict[str, int] = {}
        self._status: DevinStatus | None = None
        self._status_at = 0.0
        self._login: _Login | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ listeners

    def add_listener(self, callback: Callable[[DevinController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[DevinController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, controller: DevinController, kind: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(controller, kind)
            except Exception:
                log.exception("devin listener failed")

    def add_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """``callback(account_json)`` after a sign-in or a sign-out."""
        self._account_listeners.append(callback)

    def remove_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._account_listeners:
            self._account_listeners.remove(callback)

    def _emit_account(self, account: dict[str, Any]) -> None:
        for callback in list(self._account_listeners):
            try:
                callback(dict(account))
            except Exception:
                log.exception("devin account listener failed")

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ the process

    async def binary(self, *, refresh: bool = False) -> DevinBinary | None:
        """The Devin CLI in use (located once per ``devin_path`` setting; ``refresh`` looks again)."""
        explicit = (self.settings.devin_path or "").strip() or None
        if refresh or not self._located or self._binary_for != explicit or self._binary is None:
            self._binary = await asyncio.to_thread(self._locate, explicit)
            self._binary_for = explicit
            self._located = True
        return self._binary

    @property
    def running(self) -> bool:
        return self._client is not None and self._client.running

    async def path_changed(self) -> bool:
        """``devin_path`` was changed. What was read from the old program (account, model list, windows) is stale, and so
        is its process: it is stopped when no session uses it, so that the next request starts the new program.

        ``False``: sessions are open and keep the old process until they are closed (stopping it would end them)."""
        self._models = None
        self._status = None
        self._windows.clear()
        if self.open_controllers():
            return False
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            self._retired.append(client)
            await client.close()
        return True

    async def connection(self) -> AcpClient:
        """``devin acp``, started and initialized on first use (and again after it died)."""
        async with self._client_lock:
            client = self._client
            if client is not None and client.running:
                return client
            self._client = None
            binary = await self.binary()
            if binary is None:
                raise DevinNotFoundError()
            route = _Route(self)
            client = self._client_factory(binary, on_notification=route.notification, on_request=route.request, on_exit=route.exit)
            route.client = client
            await client.start()
            try:
                await client.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        # ``elicitation``: Devin asks the user questions (its ``ask_user_question``) only of a client that
                        # says it can show them (ACP: each mode named explicitly); without it Devin never asks.
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                            "elicitation": {"form": {}, "url": {}},
                        },
                        "clientInfo": dict(CLIENT_INFO),
                    },
                    timeout=INITIALIZE_SECONDS,
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    await client.close()
                raise
            log.info("devin acp %s started (pid %s)", binary.version, client.pid)
            self._client = client
            return client

    def _lost(self, client: AcpClient | None, error: Any) -> None:
        log.warning("devin acp exited: %s %s", error, getattr(error, "stderr", "")[-500:])
        if self._client is client:
            self._client = None
        for controller in list(self.controllers):
            if controller._client is client:
                controller._connection_lost(error)
        if client is not None:
            self._retired.append(client)
            self._spawn(client.close())  # releases the dead process's pipes and tasks

    def _route_notification(self, client: AcpClient | None, method: str, params: Any) -> None:
        params = params if isinstance(params, Mapping) else {}
        controller = self._sessions.get(str(params.get("sessionId") or ""))
        if controller is not None and controller._client is client:
            controller._on_notification(method, params)

    async def _route_request(self, client: AcpClient | None, method: str, params: Any, request_id: Any) -> Any:
        params = params if isinstance(params, Mapping) else {}
        controller = self._sessions.get(str(params.get("sessionId") or ""))
        if controller is not None and controller._client is client:
            return await controller._on_server_request(method, params, request_id)
        if method == "session/request_permission":
            return CANCELLED  # a session nobody shows (any more)
        raise DevinRpcError(-32601, f"no open WittyCode session for {method}")

    # ------------------------------------------------------------------ the command line

    async def _cli(self, *arguments: str, timeout: float = STATUS_SECONDS) -> tuple[int, str]:
        """Run the CLI to the end: ``(exit code, what it printed)``."""
        binary = await self.binary()
        if binary is None:
            raise DevinNotFoundError()
        options: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        process = await asyncio.create_subprocess_exec(
            *binary.argv(*arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self._cli_env,
            **options,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._reap(process)
            raise DevinProcessError(f"devin {' '.join(arguments)} did not finish within {timeout:g} s") from None
        except BaseException:
            # (cancelled, e.g. at shutdown: a process left running keeps its pipes open past the end of the event loop)
            await self._reap(process)
            raise
        return process.returncode or 0, out.decode("utf-8", "replace")

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), 5.0)

    # ------------------------------------------------------------------ the account

    def cached_account(self) -> dict[str, Any]:
        """The account as last read (no process started)."""
        return account_json(self._binary, self._status, signing_in=self._login is not None)

    async def account(self) -> dict[str, Any]:
        """The account for the page (without ``enabled``)."""
        binary = await self.binary()
        if binary is None:
            return account_json(None)
        try:
            code, text = await self._cli("auth", "status")
        except DevinError as error:
            return account_json(binary, self._status, signing_in=self._login is not None, error=friendly_failure(error))
        self._status = parse_status(text) if code == 0 else DevinStatus()
        self._status_at = self._clock()
        return account_json(binary, self._status, signing_in=self._login is not None)

    async def ensure_signed_in(self) -> None:
        """Refuse to open a session without credentials: its prompts would only fail, and ``session/new`` would only warn."""
        recent = self._status is not None and self._status.signed_in and self._clock() - self._status_at < SIGNED_IN_CACHE_SECONDS
        if not recent:
            await self.account()
        if self._status is None or not self._status.signed_in:
            raise DevinSignedOutError()

    async def login(self) -> dict[str, Any]:
        """Sign in with the CLI's browser flow (``devin auth login`` opens the browser itself and ends when it is done);
        returns the account once signed in, cancelled or timed out (5 min)."""
        current = self._login
        if current is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(current.done), self.login_timeout)
            return await self.account()
        binary = await self.binary()
        if binary is None:
            raise DevinNotFoundError()
        options: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        process = await asyncio.create_subprocess_exec(
            *binary.argv("auth", "login"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self._cli_env,
            **options,
        )
        attempt = _Login(process)
        self._login = attempt
        self._emit_account(self.cached_account())
        output = self._spawn(self._drain(process))
        self._spawn(self._watch(attempt))
        try:
            try:
                await asyncio.wait_for(asyncio.shield(attempt.done), self.login_timeout)
            except asyncio.TimeoutError:
                await self._stop(attempt)
        finally:
            if self._login is attempt:
                self._login = None
            # (the pipe is read to its end before it is let go: a transport dropped open is closed late, by the collector)
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(output, 2.0)
            output.cancel()
        account = await self.account()
        self._emit_account(account)
        return account

    @staticmethod
    async def _drain(process: asyncio.subprocess.Process) -> None:
        """The CLI waits for nobody to read what it prints, but a full pipe would stop it."""
        if process.stdout is not None:
            with contextlib.suppress(Exception):
                while await process.stdout.read(4096):
                    pass

    @staticmethod
    async def _watch(attempt: _Login) -> None:
        attempt.finish(await attempt.process.wait())

    async def _stop(self, attempt: _Login) -> None:
        if self._login is attempt:
            self._login = None
        process = attempt.process
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
        attempt.finish(None)

    async def cancel_login(self) -> dict[str, Any]:
        attempt = self._login
        if attempt is not None:
            await self._stop(attempt)
        account = await self.account()
        self._emit_account(account)
        return account

    async def logout(self) -> dict[str, Any]:
        await self._cli("auth", "logout")
        self._status = DevinStatus()
        account = await self.account()
        self._emit_account(account)
        return account

    # ------------------------------------------------------------------ models

    def _scratch(self) -> str:
        folder = Path(tempfile.gettempdir()) / "WittyCode" / "devin-scratch"
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    def open_external(self, url: str) -> None:
        """Open a page in the user's browser, after the user agreed to (a URL-mode elicitation: a sign-in Devin asks for)."""
        if url.lower().startswith(("http://", "https://")):
            self._open_url(url)

    async def list_models(self, *, refresh: bool = False) -> list[DevinModel]:
        """The account's models, with the thought levels each offers (cached; ``refresh`` asks again).

        They come from a scratch session (made and deleted again): its ``model`` select lists them, and choosing each one
        in turn shows that model's ``thought_level`` select (about 60 ms each)."""
        async with self._models_lock:
            if self._models is None or refresh:
                client = await self.connection()
                created = await client.request("session/new", {"cwd": self._scratch(), "mcpServers": []}, timeout=NEW_SESSION_SECONDS)
                created = created if isinstance(created, Mapping) else {}
                session_id = str(created.get("sessionId") or "")
                entries = model_entries(created.get("configOptions"))
                levels: dict[str, tuple[tuple[str, ...], str]] = {}
                try:
                    for model_id, _, _ in entries:
                        try:
                            after = await client.request(
                                "session/set_config_option",
                                {"sessionId": session_id, "configId": "model", "value": model_id},
                                timeout=REQUEST_SECONDS,
                            )
                        except DevinRpcError:
                            continue
                        levels[model_id] = thought_levels(after.get("configOptions") if isinstance(after, Mapping) else None)
                finally:
                    if session_id:
                        with contextlib.suppress(DevinError):
                            await client.request("session/delete", {"sessionId": session_id}, timeout=REQUEST_SECONDS)
                self._models = build_models(entries, levels, await self._cli_models())
            return [self._with_window(model) for model in self._models]

    async def _cli_models(self) -> dict[str, Any]:
        """Context windows and cost tiers from ``devin models list --format json`` (best effort)."""
        try:
            code, text = await self._cli("models", "list", "--format", "json")
            return cli_info(json.loads(text)) if code == 0 else {}
        except (DevinError, ValueError):
            return {}

    def memory_servers(self, controller: DevinController) -> list[dict[str, Any]]:
        """``mcpServers`` of ``session/new`` and ``session/load``: the shared memory's server (ACP's stdio form; the
        environment is a list of name / value pairs). Empty when the memory is off."""
        command = self.memory.server_for("devin", controller.project, controller.run) if self.memory is not None else None
        if command is None:
            return []
        env = [{"name": name, "value": value} for name, value in command.env.items()]
        return [{"name": command.name, "command": command.command, "args": list(command.args), "env": env}]

    def _with_window(self, model: DevinModel) -> DevinModel:
        window = self._windows.get(model.id)
        return replace(model, context_window=window) if window and not model.context_window else model

    def model_info(self, model_id: str) -> DevinModel | None:
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

    async def list_sessions(self, project: str | Path, limit: int | None = SESSION_LIST_LIMIT) -> list[DevinSavedSession]:
        """Saved sessions of ``project`` that have a message, most recently used first ([] when Devin is not available)."""
        folder = normalize_folder(project)
        found: dict[str, DevinSavedSession] = {}
        try:
            client = await self.connection()
            for variant in cwd_variants(folder):
                result = await client.request("session/list", {"cwd": variant}, timeout=REQUEST_SECONDS)
                for raw in (result.get("sessions") if isinstance(result, Mapping) else None) or ():
                    if not isinstance(raw, Mapping) or not raw.get("sessionId"):
                        continue
                    meta = raw.get("_meta") if isinstance(raw.get("_meta"), Mapping) else {}
                    if meta.get("cognition.ai/hasUserMessages") is False:
                        continue  # (made and never used: a scratch session, or one that was opened and left)
                    found.setdefault(str(raw["sessionId"]), saved_session(raw))
        except Exception as error:
            log.info("listing devin sessions failed: %s", error)
            if not found:
                return []
        ordered = sorted(found.values(), key=lambda entry: entry.modified_at, reverse=True)
        return ordered[:limit] if limit else ordered

    def find(self, session_id: str) -> DevinController | None:
        return next((c for c in self.controllers if c.session_id == session_id and not c._close_requested), None)

    def open_controllers(self) -> list[DevinController]:
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
    ) -> DevinController:
        """Start a session in ``project``. ``transcript`` / ``handoff`` continue a conversation from another engine.

        A failure leaves the controller CLOSED with ``last_error`` (and an error block in the transcript), like the own loop's."""
        controller = DevinController(
            manager=self,
            project=Path(normalize_folder(project)),
            requested_model=str(model or ""),
            reasoning=str(getattr(reasoning, "value", reasoning) or ""),
            autonomy=normalize_level(autonomy) or normalize_level(self.settings.devin_autonomy) or DEFAULT_LEVEL,
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
        self, project: str | Path, session_id: str, *, title: str | None = None, modified: float | None = None
    ) -> DevinController:
        """Resume a session. ``modified`` (epoch seconds): when it last had a message, kept as its activity time."""
        existing = self.find(session_id)
        if existing is not None:
            return existing
        leaving = next((c for c in self.controllers if c.session_id == session_id and c._close_requested), None)
        if leaving is not None:
            await leaving.close()
        controller = DevinController(
            manager=self,
            project=Path(normalize_folder(project)),
            session_id=session_id,
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

    async def close(self, controller: DevinController) -> None:
        await controller.close()

    async def close_all(self, timeout: float = 20.0) -> None:
        """Close every session, then the process."""
        controllers = list(self.controllers)
        if controllers:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(c.close() for c in controllers), return_exceptions=True), timeout)
        self.controllers.clear()
        self._sessions.clear()
        if self._login is not None:
            await self._stop(self._login)
            self._login = None
        if self._tasks:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.close()
        retired, self._retired = self._retired, []
        if retired:
            await asyncio.gather(*(old.close() for old in retired), return_exceptions=True)

    # ------------------------------------------------------------------ bookkeeping (controllers call these)

    def _attach(self, controller: DevinController) -> None:
        if controller.session_id:
            self._sessions[controller.session_id] = controller

    def _detach(self, controller: DevinController) -> None:
        for session_id, owner in list(self._sessions.items()):
            if owner is controller:
                del self._sessions[session_id]

    def _forget(self, controller: DevinController) -> None:
        if controller in self.controllers:
            self.controllers.remove(controller)


__all__ = [
    "CLIENT_INFO",
    "DevinEngine",
    "DevinSavedSession",
    "cwd_variants",
    "default_client_factory",
    "saved_session",
]
