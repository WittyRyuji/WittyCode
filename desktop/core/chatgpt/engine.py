"""The ChatGPT Plan engine: the user's ChatGPT plan, run by WittyCode's own agent loop, next to ``CodexEngine`` and ``DevinEngine``.

It owns what is common to all the conversations of this kind: the signed-in accounts (``accounts``, OAuth tokens that renew
themselves), the model list, the saved conversations (``harness.store``) and the open controllers. There is no process to
manage: a request is an HTTPS call made when a message is sent, so opening a conversation is instant and nothing can die.

Sign-in is OpenAI's "Sign in with ChatGPT" for open-source apps (``oauth``): a browser tab, a loopback callback, PKCE, and
the permission to use the plan (``chatgpt.tokens.use.direct``). A sign-in without that permission is kept but marked: the
page offers to ask again, and no request is made with it.

``stream`` is the one function the loop calls to ask the model: it takes a fresh access token, posts the request and reads
the events. ``WITTYCODE_CHATGPT_SHAPE`` (a JSON object of ``responses.Shape`` fields) changes what the request looks like
without a new build, for the parts of the route that were not verified on a real account.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
import webbrowser
from collections.abc import AsyncIterator, Callable
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop.core.chatgpt.accounts import AccountStore, TokenProvider
from desktop.core.chatgpt.errors import AuthError, ChatGPTError, PlanNotEnabled
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.models import FALLBACK_MODELS, ChatGPTModel, models_from_result
from desktop.core.chatgpt.oauth import Endpoints, SignIn
from desktop.core.chatgpt.responses import BASE_URL, Event, Shape, for_responses, read_events
from desktop.core.harness.store import SavedRow, SessionStore
from desktop.core.harness.tools import ToolRegistry
from desktop.core.handoff import Handoff
from desktop.core.hooks import Hooks
from desktop.core.llm.backend import ApiBackend, is_api_model, model_of
from desktop.core.settings import AppSettings, normalize_folder
from desktop.core.transcript import Transcript

if TYPE_CHECKING:
    from desktop.core.chatgpt.controller import ChatGPTController
    from desktop.core.memory.launch import MemoryLaunch

log = logging.getLogger("desktop.chatgpt")

APP_NAME = "WittyCode"
SHAPE_ENV = "WITTYCODE_CHATGPT_SHAPE"
LOGIN_TIMEOUT_SECONDS = 300.0
SESSION_LIST_LIMIT = 60
MODELS_FRESH_SECONDS = 600.0


def shape_from_env(env: dict[str, str] | None = None) -> Shape:
    """``Shape`` with the fields ``WITTYCODE_CHATGPT_SHAPE`` names (a JSON object); a bad value is ignored."""
    raw = (env if env is not None else os.environ).get(SHAPE_ENV, "").strip()
    if not raw:
        return Shape()
    try:
        values = json.loads(raw)
    except ValueError:
        log.warning("%s is not JSON: ignored", SHAPE_ENV)
        return Shape()
    known = {field.name for field in fields(Shape)}
    if not isinstance(values, dict) or not set(values) <= known:
        log.warning("%s must be an object with fields of Shape (%s): ignored", SHAPE_ENV, ", ".join(sorted(known)))
        return Shape()
    return Shape(**values)


def signed_out_view() -> dict[str, Any]:
    return {"auth": "signed-out", "email": "", "planEnabled": False, "accounts": [], "clientId": ""}


class ChatGPTEngine:
    def __init__(
        self,
        settings: AppSettings,
        root: str | os.PathLike[str],
        *,
        http: HttpClient | None = None,
        endpoints: Endpoints | None = None,
        base_url: str = BASE_URL,
        open_browser: Callable[[str], Any] | None = None,
        shape: Shape | None = None,
        clock: Callable[[], float] = time.time,
        login_timeout: float = LOGIN_TIMEOUT_SECONDS,
        api: ApiBackend | None = None,
    ) -> None:
        self.settings = settings
        self.api = api
        """The API providers' models (``core/llm``): a request for one of them goes there, to the provider itself, and needs no sign-in."""
        self.root = Path(root)
        self.http = http or HttpClient()
        self.endpoints = endpoints or Endpoints.default()
        self.base_url = base_url.rstrip("/")
        self._open_browser = open_browser or webbrowser.open
        self.shape = shape or shape_from_env()
        self._clock = clock
        self.login_timeout = login_timeout
        self.accounts = AccountStore(self.root)
        self.tokens = TokenProvider(self.accounts, self.http, self.endpoints, clock=clock)
        self.sessions = SessionStore(self.root)
        self.registry = ToolRegistry.default()
        self.controllers: list[ChatGPTController] = []
        self.memory: MemoryLaunch | None = None
        """The shared memory, when there is one: every conversation is given its MCP server."""
        self.hooks = Hooks()
        self._listeners: list[Callable[[ChatGPTController, str], None]] = []
        self._account_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._models: list[ChatGPTModel] | None = None
        self._models_at = 0.0
        self._models_for: str | None = None
        self._models_lock = asyncio.Lock()
        self._windows: dict[str, int] = {}
        self._signin: SignIn | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ listeners

    def add_listener(self, callback: Callable[[ChatGPTController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ChatGPTController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, controller: ChatGPTController, kind: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(controller, kind)
            except Exception:
                log.exception("chatgpt listener failed")

    def add_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._account_listeners.append(callback)

    def remove_account_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        if callback in self._account_listeners:
            self._account_listeners.remove(callback)

    def _emit_account(self) -> None:
        view = self.cached_account()
        for callback in list(self._account_listeners):
            try:
                callback(dict(view))
            except Exception:
                log.exception("chatgpt account listener failed")

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ the account

    def cached_account(self) -> dict[str, Any]:
        """The account for the page. Reads the credential files only (no network)."""
        records = self.accounts.accounts()
        active_id = self.accounts.active_id()
        active = next((r for r in records if r.client_id == active_id), None)
        view = signed_out_view()
        view["accounts"] = [
            {"clientId": r.client_id, "label": r.label, "signedIn": r.signed_in, "planEnabled": r.plan_enabled, "active": r.client_id == active_id}
            for r in records
        ]
        if active is not None and active.signed_in:
            view.update({"auth": "signed-in", "email": active.email, "planEnabled": active.plan_enabled, "clientId": active.client_id})
        elif active is not None:
            view.update({"email": active.email, "clientId": active.client_id})
        if self._signin is not None:
            view["signingIn"] = True
        return view

    async def account(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.cached_account)

    async def ensure_ready(self) -> None:
        """Refuse to open a conversation that could only fail: nobody signed in, or the plan not allowed."""
        view = await self.account()
        if view["auth"] != "signed-in":
            raise AuthError("not signed in to ChatGPT", terminal=True)
        if not view["planEnabled"]:
            raise PlanNotEnabled("the ChatGPT plan is not allowed for this app", terminal=False)

    async def login(self, *, add: bool = False, force_consent: bool = False) -> dict[str, Any]:
        """Sign in with the browser. ``add``: a new account instead of signing the current one in again.
        ``force_consent``: ask again for the permission to use the plan (a sign-in that lacked it)."""
        if self._signin is not None:
            raise ChatGPTError("A sign-in is already in progress.", code="busy")
        current = None if add else next((r for r in self.accounts.accounts() if r.client_id == self.accounts.active_id()), None)
        flow = SignIn(
            self.http,
            self.endpoints,
            host_id=await asyncio.to_thread(self.accounts.host_id),
            app_name=APP_NAME,
            client_id=current.client_id if current is not None else None,
            id_token_hint=(current.id_token or None) if current is not None else None,
            login_hint=(current.email or None) if current is not None else None,
            open_browser=self._open_browser,
            force_consent=force_consent,
        )
        self._signin = flow
        self._emit_account()
        try:
            record = await flow.run(self.login_timeout)
        finally:
            self._signin = None
        await asyncio.to_thread(self._keep, record)
        self._models = None
        self._emit_account()
        return self.cached_account()

    def _keep(self, record: Any) -> None:
        self.accounts.save(record)
        self.accounts.set_active(record.client_id)

    async def cancel_login(self) -> dict[str, Any]:
        flow = self._signin
        if flow is not None:
            flow.cancel()
        return self.cached_account()

    async def use_account(self, client_id: str) -> dict[str, Any]:
        """Make a saved account the one conversations are paid by."""
        if self.accounts.get(client_id) is None:
            raise ChatGPTError("That account is not saved.", code="unknown_account")
        await asyncio.to_thread(self.accounts.set_active, client_id)
        self._models = None
        self._emit_account()
        return self.cached_account()

    async def logout(self, client_id: str | None = None) -> dict[str, Any]:
        """Sign an account out: its tokens are revoked at OpenAI (best effort) and always cleared here."""
        target = client_id or self.accounts.active_id()
        if target is not None and self.accounts.get(target) is not None:
            confirmed = await self.tokens.sign_out(target)  # (after a renewal in progress, which must not be cut short)
            if not confirmed:
                log.warning("OpenAI did not confirm that the sign-in was revoked; the tokens were cleared here")
        self._models = None
        self._emit_account()
        return self.cached_account()

    # ------------------------------------------------------------------ talking to OpenAI

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[Event]:
        """Ask the model: a fresh access token, the request, the events of the answer. A model of an API provider is asked at the
        provider, with its own key."""
        if self.api is not None and is_api_model(str(request.get("model") or "")):
            async for event in self.api.stream(request):
                yield event
            return
        client_id = self.accounts.active_id()
        token = await self.tokens.access_token(client_id)
        body = {**request, "input": for_responses(request.get("input") or ())}
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream", "originator": APP_NAME.lower()}
            lines = self.http.stream_lines("POST", f"{self.base_url}/responses", headers=headers, json_body=body)
            begun = False
            retry = False
            try:
                async for event in read_events(lines):
                    begun = True
                    yield event
            except ChatGPTError as error:
                if error.status != 401 or attempt or begun:
                    raise
                retry = True
            finally:
                await lines.aclose()
            if not retry:
                return
            token = await self.tokens.access_token(client_id, rejected=token)

    # ------------------------------------------------------------------ models

    async def list_models(self, *, refresh: bool = False) -> list[ChatGPTModel]:
        """The models the account can run (the documented list; the fallback when it cannot be read)."""
        active = self.accounts.active_id()
        async with self._models_lock:
            fresh = self._models is not None and self._models_for == active and self._clock() - self._models_at < MODELS_FRESH_SECONDS
            if fresh and not refresh:
                return list(self._models or ())
            models = await self._read_models()
            self._models, self._models_at, self._models_for = models, self._clock(), active
            for model in models:
                if model.context_window:
                    self._windows[model.id] = model.context_window
            return list(models)

    async def _read_models(self) -> list[ChatGPTModel]:
        try:
            token = await self.tokens.access_token()
            response = await self.http.request("GET", f"{self.base_url}/models", headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
        except ChatGPTError as error:
            log.warning("the ChatGPT model list could not be read: %s", error)
            return list(FALLBACK_MODELS)
        if response.status != 200:
            log.warning("the ChatGPT model list answered HTTP %s: %.200s", response.status, response.text())
            return list(FALLBACK_MODELS)
        try:
            models = models_from_result(response.json())
        except ValueError:
            models = []
        return models or list(FALLBACK_MODELS)

    def model_info(self, model_id: str) -> ChatGPTModel | None:
        if self.api is not None and is_api_model(model_id):
            spec = self.api.spec(model_id)
            return None if spec is None else model_of(spec)
        return next((m for m in self._models or FALLBACK_MODELS if m.id == model_id), None)

    def model_window(self, model_id: str) -> int | None:
        if self.api is not None and is_api_model(model_id):
            spec = self.api.spec(model_id)
            return None if spec is None else spec.window
        return self._windows.get(model_id)

    # ------------------------------------------------------------------ conversations

    async def list_sessions(self, project: str | Path, limit: int | None = SESSION_LIST_LIMIT) -> list[SavedRow]:
        return await asyncio.to_thread(self.sessions.rows, str(project), limit)

    def find(self, session_id: str) -> ChatGPTController | None:
        return next((c for c in self.controllers if c.session_id == session_id and not c._close_requested), None)

    def open_controllers(self) -> list[ChatGPTController]:
        return [c for c in self.controllers if c.is_open]

    def memory_servers(self, controller: ChatGPTController) -> list[Any]:
        command = self.memory.server_for("chatgpt", controller.project, controller.run) if self.memory is not None else None
        commands = [command] if command is not None else []
        connections = getattr(self, "connections", None)
        if connections is not None:
            commands += connections.mcp_commands(controller.project)
        return commands

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
    ) -> ChatGPTController:
        from desktop.core.chatgpt.controller import DEFAULT_LEVEL, PLAN, ChatGPTController, normalize_level

        if not is_api_model(str(model or "")):  # (an API provider's model needs its key, not a ChatGPT sign-in)
            await self.ensure_ready()
        controller = ChatGPTController(
            manager=self,
            project=Path(normalize_folder(project)),
            session_id=uuid.uuid4().hex,
            requested_model=str(model or ""),
            reasoning=str(getattr(reasoning, "value", reasoning) or ""),
            autonomy=normalize_level(autonomy) or normalize_level(self.settings.chatgpt_autonomy) or DEFAULT_LEVEL,
            mode=PLAN if str(getattr(mode, "value", mode) or "") == PLAN else "auto",
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
        return controller

    async def open_saved(
        self, project: str | Path, session_id: str, *, title: str | None = None, modified: float | None = None
    ) -> ChatGPTController:
        from desktop.core.chatgpt.controller import ChatGPTController

        existing = self.find(session_id)
        if existing is not None:
            return existing
        leaving = next((c for c in self.controllers if c.session_id == session_id and c._close_requested), None)
        if leaving is not None:
            await leaving.close()
        controller = ChatGPTController(
            manager=self, project=Path(normalize_folder(project)), session_id=session_id, saved_title=title or None
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

    async def close(self, controller: ChatGPTController) -> None:
        await controller.close()

    async def close_all(self, timeout: float = 20.0) -> None:
        controllers = list(self.controllers)
        if controllers:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(c.close() for c in controllers), return_exceptions=True), timeout)
        self.controllers.clear()
        flow = self._signin
        if flow is not None:
            flow.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _forget(self, controller: ChatGPTController) -> None:
        if controller in self.controllers:
            self.controllers.remove(controller)


__all__ = ["APP_NAME", "ChatGPTEngine", "shape_from_env"]
