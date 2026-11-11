"""One small question to the Codex subscription outside every conversation: the memory curator asks what a turn holds
that is worth keeping (``core/memory/curator``; ``chatgpt/side`` is the same for the ChatGPT plan, ``devin/side`` for
Devin).

Nothing of it is left in the user's lists:

- the thread is ``ephemeral``: Codex keeps it in memory only (verified on codex-cli 0.166.0-alpha.6: ``thread/start``
  answers ``ephemeral: true, path: null``, no rollout file appears and ``thread/list`` does not list it). A Codex that
  does not answer ``ephemeral: true`` (one that does not know the flag would keep the thread) is not asked at all, and
  the empty thread it made is deleted again (``thread/delete``);
- it is made in a scratch folder of WittyCode's own, not in the project: no project's ``AGENTS.md`` goes into the
  question, and no project would list it;
- it is unsubscribed at the end, also after a failure, a timeout or a cancellation; requests still on their way then
  are waited for a moment, so that a thread being made or a turn being started is let go of as well (the app-server
  unloads an unsubscribed thread after 30 idle minutes).

It is no ``CodexController``: no transcript, no listener of the application hears of it, no shared-memory MCP server
(``CodexEngine.memory_params`` is not asked), no application hook. It runs on the cheapest model the account lists
(``CHEAP``; else the default), at the weakest effort that model offers, read-only (``read-only`` sandbox, approval policy
``never``), and whatever Codex still asks for is refused. The MCP servers and plugins of the user's own Codex
configuration start for it as for every thread (``config.mcp_servers = {}`` does not turn them off: measured).

The engine's app-server is used only when it already runs: a side question never starts Codex and never signs anyone in.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import tempfile
from collections.abc import Coroutine, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop.core.codex.controller import SERVICE_NAME, THREAD_OPEN_SECONDS, TURN_START_SECONDS, UNSUBSCRIBE_SECONDS
from desktop.core.codex.errors import CodexError, CodexProcessError, CodexRpcError
from desktop.core.codex.items import turn_input
from desktop.core.codex.models import CodexModel
from desktop.core.codex.policy import sandbox_policy

if TYPE_CHECKING:
    from desktop.core.codex.engine import CodexEngine
    from desktop.core.codex.rpc import JsonRpcClient

log = logging.getLogger("desktop.codex.side")

SECONDS = 90.0
CHEAP = ("nano", "mini", "luna", "lite", "flash")
"""Words in the ids of small, fast models, the cheapest kind first (the subscription lists ``gpt-6-luna``, "Fast and
affordable", next to the ``sol`` workhorses). A word counts only as a whole part of the id (``gemini`` is no ``mini``)."""
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
"""Reasoning efforts (and Devin's thought levels), weakest first."""
APPROVAL = "never"
SANDBOX = "read-only"
LEVEL = "readonly"
INSTRUCTIONS = (
    "This is a one-off question from the WittyCode application, outside any conversation. Answer it directly in the "
    "requested format, without running commands, editing files or calling tools."
)
REFUSALS: dict[str, dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline", "content": None, "_meta": None},
    "item/tool/call": {"contentItems": [], "success": False},
}
"""The answer to whatever Codex asks a side question for (with the ``never`` policy it should ask nothing): no."""
METHOD_NOT_FOUND = -32601
WATCH_SECONDS = 0.25
"""How often the wait for the turn's end looks whether the process still runs (the engine tells only controllers)."""
SETTLE_SECONDS = 5.0
"""How long the clean-up waits for a request still on its way (the thread being made, the turn being started)."""
INTERRUPT_SECONDS = 2.0


def words(model_id: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", model_id.lower()))


def cheapness(model_id: str) -> int:
    """Where the model's kind stands in ``CHEAP`` (``len(CHEAP)``: none of those words is in its id)."""
    found = words(model_id)
    return next((rank for rank, word in enumerate(CHEAP) if word in found), len(CHEAP))


def side_model(models: Sequence[CodexModel]) -> CodexModel | None:
    """The cheapest model the account lists (the first one of the cheapest kind), else its default, else the first."""
    cheap = [model for model in models if cheapness(model.id) < len(CHEAP)]
    if cheap:
        return min(cheap, key=lambda model: cheapness(model.id))
    return next((model for model in models if model.is_default), None) or (models[0] if models else None)


def lowest_effort(efforts: Sequence[str]) -> str:
    """The weakest of the efforts a model offers ("": it offers none, its own default applies)."""
    known = [effort for effort in efforts if effort in EFFORTS]
    if known:
        return min(known, key=EFFORTS.index)
    return efforts[0] if efforts else ""


def scratch() -> Path:
    """Where side questions run: an empty folder of WittyCode's own (``DevinEngine._scratch`` has Devin's)."""
    folder = Path(tempfile.gettempdir()) / "WittyCode" / "codex-scratch"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def settle(request: asyncio.Future[Any] | None, seconds: float) -> Any:
    """What a request still on its way answers, if it does within ``seconds`` (``None`` otherwise: then it is dropped)."""
    if request is None:
        return None
    if not request.done():
        await asyncio.wait({request}, timeout=seconds)
    if not request.done():
        request.cancel()
        return None
    if request.cancelled() or request.exception() is not None:
        return None
    return request.result()


async def finish(engine: Any, cleanup: Coroutine[Any, Any, None]) -> None:
    """Run ``cleanup`` to its end, also when the question is cancelled again meanwhile (it then goes on as one of the
    engine's tasks); only the engine closing stops it early, and its process is going then anyway. Never raises but
    the question's own cancellation."""
    task = engine._spawn(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        log.warning("cleaning up after a side question failed", exc_info=True)


def reason(error: BaseException, timeout: float) -> str:
    """For the log: why a side question got no answer."""
    if isinstance(error, TimeoutError) and not str(error):
        return f"no answer within {timeout:g} s"
    return f"{type(error).__name__}: {error}"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _thread_id(result: Any) -> str:
    thread = result.get("thread") if isinstance(result, Mapping) else None
    return _text(thread.get("id")) if isinstance(thread, Mapping) else ""


def _ephemeral(result: Any) -> bool:
    thread = result.get("thread") if isinstance(result, Mapping) else None
    return isinstance(thread, Mapping) and thread.get("ephemeral") is True


def _turn_id(result: Any) -> str:
    turn = result.get("turn") if isinstance(result, Mapping) else None
    return _text(turn.get("id")) if isinstance(turn, Mapping) else ""


class _Question:
    """The side thread, in the engine's thread table where a controller would be: ``CodexEngine._route_notification`` and
    ``_route_request`` hand it what comes for its ``threadId`` on ``_client``."""

    def __init__(self, engine: CodexEngine, client: JsonRpcClient) -> None:
        self.engine = engine
        self._client = client
        self.session_id: str | None = None
        """The thread id (the name the engine's table knows controllers by)."""
        self.turn_id: str | None = None
        self.starting: asyncio.Future[Any] | None = None
        self.turning: asyncio.Future[Any] | None = None
        self.kept = False
        """Codex made the thread but would keep it (it is not ephemeral): it is deleted again, before it has a turn."""
        self.ended = asyncio.Event()
        self.status = ""
        self.error = ""
        self._messages: list[str] = []
        self._texts: dict[str, str] = {}
        self._phases: dict[str, str] = {}

    async def run(self, prompt: str) -> str | None:
        engine, client = self.engine, self._client
        await engine.ensure_signed_in(client)
        model = side_model(await engine.list_models())
        folder = scratch()
        params: dict[str, Any] = {
            "cwd": str(folder),
            "approvalPolicy": APPROVAL,
            "sandbox": SANDBOX,
            "serviceName": SERVICE_NAME,
            "ephemeral": True,
            "developerInstructions": INSTRUCTIONS,
        }
        if model is not None:
            params["model"] = model.id
        self.starting = asyncio.ensure_future(client.request("thread/start", params, timeout=THREAD_OPEN_SECONDS))
        started = await asyncio.shield(self.starting)
        self.session_id = _thread_id(started) or None
        if self.session_id is None:
            raise CodexError("thread/start returned no thread id")
        if not _ephemeral(started):
            self.kept = True
            log.info("this Codex would keep the side question's thread (no ephemeral threads): not asked")
            return None
        engine._attach(self)
        turn: dict[str, Any] = {
            "threadId": self.session_id,
            "input": turn_input(prompt),
            "cwd": str(folder),
            "approvalPolicy": APPROVAL,
            "sandboxPolicy": sandbox_policy(LEVEL, folder),
        }
        if model is not None:
            turn["model"] = model.id
            effort = lowest_effort(model.efforts)
            if effort:
                turn["effort"] = effort
        self.turning = asyncio.ensure_future(client.request("turn/start", turn, timeout=TURN_START_SECONDS))
        answered = await asyncio.shield(self.turning)
        self.turn_id = self.turn_id or _turn_id(answered) or None
        await self._until_ended()
        if self.status != "completed":
            log.info("the side question's turn ended %s%s", self.status, f" ({self.error})" if self.error else "")
            return None
        return self.answer() or None

    async def _until_ended(self) -> None:
        while not self.ended.is_set():
            if not self._client.running:
                raise CodexProcessError("codex app-server is not running")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.ended.wait(), WATCH_SECONDS)

    def answer(self) -> str:
        """What the turn said: its agent messages but the commentary between steps (all of them if that is all)."""
        said = [(self._phases.get(item, ""), self._texts.get(item, "").strip()) for item in self._messages]
        answer = [text for phase, text in said if text and phase != "commentary"] or [text for _, text in said if text]
        return "\n\n".join(answer)

    def _message(self, item_id: str) -> None:
        if item_id not in self._messages:
            self._messages.append(item_id)

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            item_id = _text(params.get("itemId"))
            if item_id:
                self._message(item_id)
                self._texts[item_id] = self._texts.get(item_id, "") + _text(params.get("delta"))
        elif method in ("item/started", "item/completed"):
            item = params.get("item")
            if not isinstance(item, Mapping) or item.get("type") != "agentMessage" or not _text(item.get("id")):
                return
            item_id = item["id"]
            self._message(item_id)
            if _text(item.get("phase")):
                self._phases[item_id] = item["phase"]
            if method == "item/completed":
                self._texts[item_id] = _text(item.get("text")) or self._texts.get(item_id, "")
        elif method == "turn/started":
            self.turn_id = self.turn_id or _turn_id(params) or None
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
            error = turn.get("error")
            self.status = _text(turn.get("status")) or "completed"
            self.error = _text(error.get("message")) if isinstance(error, Mapping) else ""
            self.ended.set()
        elif method == "thread/closed":
            self.status = self.status or "closed"
            self.ended.set()

    async def _on_server_request(self, method: str, params: Mapping[str, Any], request_id: Any) -> Any:
        refusal = REFUSALS.get(method)
        if refusal is None:
            raise CodexRpcError(METHOD_NOT_FOUND, f"a WittyCode side question does not handle {method}")
        log.info("Codex asked the side question for %s: refused", method)
        return copy.deepcopy(refusal)

    async def clean_up(self) -> None:
        """Let go of the thread: stop its turn if that still runs (delete it if Codex would keep it), then unsubscribe."""
        client = self._client
        try:
            if self.session_id is None:
                self.session_id = _thread_id(await settle(self.starting, SETTLE_SECONDS)) or None
            if self.session_id is None or not client.running:
                return
            if self.turn_id is None:
                self.turn_id = _turn_id(await settle(self.turning, SETTLE_SECONDS)) or None
            if self.turn_id is not None and not self.ended.is_set():
                with contextlib.suppress(CodexError):
                    await client.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, timeout=INTERRUPT_SECONDS)
            if self.kept:
                with contextlib.suppress(CodexError):
                    await client.request("thread/delete", {"threadId": self.session_id}, timeout=UNSUBSCRIBE_SECONDS)
            try:
                await client.request("thread/unsubscribe", {"threadId": self.session_id}, timeout=UNSUBSCRIBE_SECONDS)
            except CodexError as error:
                log.info("the side question's thread could not be unsubscribed: %s", error)
        finally:
            self.engine._detach(self)


async def ask(engine: CodexEngine, project: str, prompt: str, *, timeout: float = SECONDS) -> str | None:
    """The model's answer to ``prompt``, or ``None`` when Codex cannot be asked now: switched off, its app-server not
    running, signed out, an error, a turn that failed or was stopped, no answer within ``timeout`` seconds (the reason is
    only logged: it is not the user's turn that failed). ``project`` is the conversation's folder; the question does not
    run there (see the module's docstring). Never raises but ``asyncio.CancelledError``."""
    client = engine._client
    if not engine.settings.codex_enabled or client is None or not client.running or not prompt.strip():
        return None
    question = _Question(engine, client)
    try:
        async with asyncio.timeout(timeout):
            return await question.run(prompt)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.info("the Codex side question was not answered: %s", reason(error, timeout))
        return None
    finally:
        await finish(engine, question.clean_up())


__all__ = ["ask", "cheapness", "finish", "lowest_effort", "reason", "scratch", "settle", "side_model"]
