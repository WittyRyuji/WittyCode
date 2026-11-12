"""One small question to the Devin account outside every conversation: the memory curator asks what a turn holds that is
worth keeping (``core/memory/curator``; ``codex/side`` is the same for Codex, and lends this module its helpers).

Nothing of it is left in the user's lists: the session is made in the scratch folder the engine reads its model list in
(``DevinEngine._scratch``) and deleted at the end (``session/delete``, as that model list's session is), also after a
failure, a timeout or a cancellation; a session still being made then is deleted once it is, and a prompt still running
is stopped (``session/cancel``) and given a moment to end first.

It is no ``DevinController``: no transcript, no listener of the application hears of it, no MCP server (``mcpServers`` is
empty, so the shared memory's server is not started), no application hook. It runs on a free model when the account
has one (the engine's model list says which), else on one of the SWE-2 family, else on Devin's default, the cheapest
kind first (``codex.side.CHEAP``), at the lowest thought level that model offers, in Devin's ``ask`` mode ("answer
questions without code changes"), and every permission Devin asks for is refused.

The engine's ``devin acp`` is used only when it already runs: a side question never starts Devin and never signs anyone in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from desktop.core.side_helpers import cheapness, finish, lowest_effort, reason, settle
from desktop.core.devin.controller import CONFIG_SECONDS, NEW_SESSION_SECONDS
from desktop.core.devin.errors import DevinError, DevinRpcError
from desktop.core.devin.items import prompt_blocks
from desktop.core.devin.models import current_value, model_entries, thought_levels
from desktop.core.devin.policy import mode_of, outcome_for

if TYPE_CHECKING:
    from desktop.core.devin.engine import DevinEngine
    from desktop.core.devin.rpc import AcpClient

log = logging.getLogger("desktop.devin.side")

SECONDS = 90.0
FREE = "Free"
"""``DevinModel.note`` of the models that cost nothing on the account (from ``devin models list``)."""
FAMILY = "swe-2"
"""Cognition's own models (free on the account seen): the choice when the engine knows of no free model (it has not read
its model list yet, or the list marks none)."""
LEVEL = "ask"
"""The permission level (``policy``): Devin's ``ask`` mode, which answers without changing code."""
METHOD_NOT_FOUND = -32601
SETTLE_SECONDS = 5.0
"""How long the clean-up waits for a session still being made."""
CANCEL_SECONDS = 3.0
"""How long a stopped prompt gets to end before its session is deleted all the same."""
DELETE_SECONDS = 10.0


def side_model(offered: Sequence[str], engine: DevinEngine) -> str | None:
    """The model of ``offered`` (the session's ``model`` select) a side question runs on: a free one, else one of the
    SWE-2 family, the cheapest kind first in either; ``None``: the session's default stays."""
    free = [model for model in offered if (info := engine.model_info(model)) is not None and info.note == FREE]
    pool = free or [model for model in offered if model.startswith(FAMILY)]
    return min(pool, key=cheapness) if pool else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _session_id(result: Any) -> str:
    return _text(result.get("sessionId")) if isinstance(result, Mapping) else ""


class _Question:
    """The side session, in the engine's session table where a controller would be: ``DevinEngine._route_notification``
    and ``_route_request`` hand it what comes for its ``sessionId`` on ``_client``."""

    def __init__(self, engine: DevinEngine, client: AcpClient) -> None:
        self.engine = engine
        self._client = client
        self.session_id: str | None = None
        self.creating: asyncio.Future[Any] | None = None
        self.asking: asyncio.Future[Any] | None = None
        self._said: list[str] = [""]
        """The words of the turn, a new piece after each tool call: the last piece is the answer."""

    async def run(self, prompt: str) -> str | None:
        engine, client = self.engine, self._client
        await engine.ensure_signed_in()
        self.creating = asyncio.ensure_future(
            client.request("session/new", {"cwd": engine._scratch(), "mcpServers": []}, timeout=NEW_SESSION_SECONDS)
        )
        created = await asyncio.shield(self.creating)
        self.session_id = _session_id(created) or None
        if self.session_id is None:
            raise DevinError("session/new returned no session id")
        engine._attach(self)
        options = created.get("configOptions")
        model = side_model([entry[0] for entry in model_entries(options)], engine)
        if model and model != current_value(options, "model"):
            try:
                options = await self._set("model", model, options)
            except DevinRpcError as error:
                log.info("Devin did not take %s for the side question (%s): its default model answers", model, error.message or error.code)
        levels, level = thought_levels(options)
        lowest = lowest_effort(levels)
        if lowest and lowest != level:
            with contextlib.suppress(DevinRpcError):
                options = await self._set("thought_level", lowest, options)
        mode = mode_of(LEVEL)
        if current_value(options, "mode") != mode:
            await client.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode}, timeout=CONFIG_SECONDS)
        self.asking = asyncio.ensure_future(
            client.request("session/prompt", {"sessionId": self.session_id, "prompt": prompt_blocks(prompt)}, timeout=None)
        )
        result = await asyncio.shield(self.asking)
        stop = _text(result.get("stopReason")) if isinstance(result, Mapping) else ""
        if stop not in ("", "end_turn"):
            log.info("the side question's turn ended: %s", stop)
            return None
        return self.answer() or None

    async def _set(self, config_id: str, value: str, before: Any) -> Any:
        result = await self._client.request(
            "session/set_config_option", {"sessionId": self.session_id, "configId": config_id, "value": value}, timeout=CONFIG_SECONDS
        )
        after = result.get("configOptions") if isinstance(result, Mapping) else None
        return after or before

    def answer(self) -> str:
        return next((piece.strip() for piece in reversed(self._said) if piece.strip()), "")

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        update = params.get("update") if method == "session/update" else None
        if not isinstance(update, Mapping):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            self._said[-1] += _text(content.get("text")) if isinstance(content, Mapping) else ""
        elif kind in ("tool_call", "tool_call_update") and self._said[-1]:
            self._said.append("")

    async def _on_server_request(self, method: str, params: Mapping[str, Any], request_id: Any) -> Any:
        if method != "session/request_permission":
            raise DevinRpcError(METHOD_NOT_FOUND, f"a WittyCode side question does not handle {method}")
        log.info("Devin asked the side question for a permission: refused")
        return outcome_for("cancel", [option for option in params.get("options") or () if isinstance(option, Mapping)])

    async def clean_up(self) -> None:
        """Delete the session (stopping its prompt first if that still runs)."""
        client = self._client
        try:
            if self.session_id is None:
                self.session_id = _session_id(await settle(self.creating, SETTLE_SECONDS)) or None
            if self.session_id is None or not client.running:
                return
            if self.asking is not None and not self.asking.done():
                with contextlib.suppress(DevinError):
                    await client.notify("session/cancel", {"sessionId": self.session_id})
                await settle(self.asking, CANCEL_SECONDS)
            try:
                await client.request("session/delete", {"sessionId": self.session_id}, timeout=DELETE_SECONDS)
            except DevinError as error:
                log.info("the side question's session could not be deleted: %s", error)
        finally:
            self.engine._detach(self)


async def ask(engine: DevinEngine, project: str, prompt: str, *, timeout: float = SECONDS) -> str | None:
    """The model's answer to ``prompt``, or ``None`` when Devin cannot be asked now: switched off, ``devin acp`` not
    running, signed out, an error, a turn that was stopped or refused, no answer within ``timeout`` seconds (the reason
    is only logged: it is not the user's turn that failed). ``project`` is the conversation's folder; the question does
    not run there (see the module's docstring). Never raises but ``asyncio.CancelledError``."""
    client = engine._client
    if not engine.settings.devin_enabled or client is None or not client.running or not prompt.strip():
        return None
    if engine.cached_account().get("auth") == "signed-out":
        return None
    question = _Question(engine, client)
    try:
        async with asyncio.timeout(timeout):
            return await question.run(prompt)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.info("the Devin side question was not answered: %s", reason(error, timeout))
        return None
    finally:
        await finish(engine, question.clean_up())


__all__ = ["ask", "side_model"]
