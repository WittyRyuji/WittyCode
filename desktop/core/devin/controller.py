"""One Devin session as an open session, with the public surface of ``SessionController`` (``core/session_manager``).

The engine (``engine.DevinEngine``) owns the one ``devin acp`` process that all sessions share and hands each controller
the notifications and requests of its session (every one carries ``sessionId``). The controller turns them into the
transcript, through the ``Transcript`` builder API only (``items`` says which tool call becomes what).

Lifecycle (``SessionState``, the same enum as Droid's sessions, so the backend treats all engines alike):

- ``open_new``: ``session/new`` (cwd), then the settings asked for (``session/set_config_option`` model and thought level,
  ``session/set_mode``) -> READY. ``open_saved``: ``session/load``, which *replays* the saved session as notifications
  (``history.Replay`` builds the turns) -> READY; the model, thought level and mode the answer reports become the settings.
- ``send_prompt``: RUNNING -> ``session/prompt`` (answered when the turn is over) with ``session/update`` notifications
  meanwhile. ``update_settings`` applies at once (``set_config_option`` / ``set_mode``).
- A prompt sent *while* a turn runs is merged into that turn (Devin's ``midTurnPrompts``; verified: both prompts' answers
  arrive together, with the same stop reason). That is ``steer``; the turn is over when every prompt sent for it has been
  answered.
- ``interrupt``: the ``session/cancel`` notification; the prompt is then answered ``cancelled``. A turn that has not ended
  ``INTERRUPT_GRACE_SECONDS`` after the stop is ended here (shown as stopped).
- The process dying: DEAD plus a recoverable error block, for every Devin session at once (they share the process);
  ``reconnect``: a new process, ``session/load`` (its replay is not shown again).

Usage: what a prompt cost comes from ``_cognition.ai/turn_stats`` (``items.stats_tokens``), one per prompt, keyed by the
id of the user message: the replay of a saved session carries the same figures under the same ids, so counting them by id
is the same whether the session ran here or was loaded. ``context`` comes from ``usage_update`` (``used`` / ``size``).

Handoff: ``open_new(..., transcript=, handoff=)`` continues a conversation another engine had; the first ``send_prompt``
sends ``handoff.prefix + text``, as the other controllers do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from desktop.core.attachments import DEFAULT_PROMPT, IMAGE, PDF, Attachment
from desktop.core.elicit import plan_of, reply_for
from desktop.core.engine_items import command_result
from desktop.core.context import ContextState
from desktop.core.devin import items
from desktop.core.devin.errors import DevinError, DevinProcessError, DevinRpcError, friendly_failure
from desktop.core.devin.history import Replay
from desktop.core.devin.models import current_value, thought_levels
from desktop.core.devin.policy import (
    CANCELLED,
    DEFAULT_LEVEL,
    level_of,
    mode_of,
    normalize_level,
    option_views,
    outcome_for,
)
from desktop.core.context_envelope import wrap as wrap_context
from desktop.core.handoff import Handoff
from desktop.core.hooks import PromptEvent
from desktop.core.memory.launch import new_run
from desktop.core.prompts import ActionView
from desktop.core.session_state import SESSION_STATE_LABELS, LiveSettings, SessionState, SteerOutcome
from desktop.core.toolinfo import working_label as label_for_working_state
from desktop.core.harness.asking import answer_text
from desktop.core.transcript import PromptStatus, ToolItem, ToolStatus, Transcript, Turn, TurnStatus, UserBlock
from desktop.core.usage import Tokens, own_output

if TYPE_CHECKING:
    from desktop.core.devin.engine import DevinEngine

log = logging.getLogger("desktop.devin")

NEW_SESSION_SECONDS = 120.0
LOAD_SECONDS = 120.0
CONFIG_SECONDS = 20.0
CLOSE_SECONDS = 5.0
INTERRUPT_GRACE_SECONDS = 8.0
OPEN_CLOSE_WAIT_SECONDS = 10.0
OUTPUT_KEEP_CHARS = 64 * 1024
METHOD_NOT_FOUND = -32601
STOP_NOTES = {
    "max_tokens": "Devin 因为达到输出上限而停止了这一轮。",
    "max_turn_requests": "Devin 因为这一轮的模型调用次数到了上限而停止。",
    "refusal": "模型拒绝了这个请求。",
}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


@dataclass(eq=False)
class DevinController:
    """Owns one Devin session: its transcript, the running turn and the pending approvals."""

    manager: DevinEngine
    project: Path
    transcript: Transcript = field(default_factory=Transcript)
    state: SessionState = SessionState.OPENING
    session_id: str | None = None
    """The Devin session id (two words, ``cliff-parrotfish``)."""
    saved_title: str | None = None
    opened_at: float = field(default_factory=time.time)
    used_at: float = field(default_factory=time.time)
    active_at: float = field(default_factory=time.time)
    """When the conversation last had a message sent or answered (the sidebar's order and time); opening or resuming
    does not change it. Same meaning as the other controllers'."""
    last_error: str = ""
    eviction_notice: str = ""
    context: ContextState = field(default_factory=ContextState)
    handoff: Handoff | None = None
    requested_model: str = ""
    reasoning: str = ""
    """The thought level (``none`` … ``max``); empty: the model has none."""
    autonomy: str = DEFAULT_LEVEL
    """``ask`` | ``plan`` | ``code`` | ``smart`` | ``full`` (``policy``)."""
    engine: ClassVar[str] = "devin"
    run: str = field(default_factory=new_run)
    """Tells the shared memory which conversation a note comes from (it is in the command of the memory server)."""
    preface: str = ""
    """The note about the shared memory put after the first message of a new session; dropped once that turn is done."""
    _preface_turn: Turn | None = None
    recalled: set[str] = field(default_factory=set)
    """What the application's hooks gave this conversation with its messages (ids of notes): not given again."""
    _recall_turn: Turn | None = None
    _recall_ids: frozenset[str] = frozenset()
    _model: str = ""
    _turn_usage: dict[str, Tokens] = field(default_factory=dict)
    _client: Any = None
    _turn: Turn | None = None
    _turn_done: asyncio.Event | None = None
    _handoff_turn: Turn | None = None
    _outstanding: int = 0
    _stop: Mapping[str, Any] | None = None
    _calls: list[Tokens] = field(default_factory=list)
    _outputs: dict[str, str] = field(default_factory=dict)
    _exits: dict[str, int] = field(default_factory=dict)
    _block: str = ""
    _blocks: int = 0
    _started: dict[str, float] = field(default_factory=dict)
    _unadopted: list[tuple[str, tuple[Attachment, ...]]] = field(default_factory=list)
    _history_loaded: bool = True
    _replay: Replay | None = None
    _pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    _requests: dict[Any, str] = field(default_factory=dict)
    _options: dict[str, Sequence[Any]] = field(default_factory=dict)
    _listeners: list[Callable[[DevinController, str], None]] = field(default_factory=list)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _close_requested: bool = False
    _close_task: asyncio.Task[None] | None = None
    _opening: asyncio.Future[None] | None = None

    # ------------------------------------------------------------------ plumbing

    def add_listener(self, callback: Callable[[DevinController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[DevinController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, kind: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(self, kind)
            except Exception:
                log.exception("session listener failed")
        self.manager._notify(self, kind)

    def _changed(self) -> None:
        self._notify("changed")

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

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
        return self._model or self.requested_model or (self.transcript.model or "")

    @property
    def usage(self) -> Tokens | None:
        """What the session's prompts cost altogether (``None`` before any was reported)."""
        total: Tokens | None = None
        for tokens in self._turn_usage.values():
            total = tokens if total is None else total.plus(tokens)
        return total

    def usage_series(self) -> dict[str, Tokens]:
        """One series per prompt, named by its user message: a figure never changes once it is reported, and the replay of
        the saved session reports the same ones, so reopening the session adds nothing twice."""
        return {f"turn:{message_id}": tokens for message_id, tokens in self._turn_usage.items()}

    def usage_pending(self) -> Tokens | None:
        """What the model calls of the running turn have cost so far (each ``usage_update``), before Devin gives the prompt's
        own figure (``turn_stats``, when the turn ends). Shown in the total meanwhile, never booked: the final figure is."""
        if self._turn is None or not self._calls:
            return None
        total = self._calls[0]
        for more in self._calls[1:]:
            total = total.plus(more)
        return total

    def _output_so_far(self) -> int | None:
        return own_output(self.usage_series())

    @property
    def working_label(self) -> str:
        if self.state is not SessionState.RUNNING:
            return SESSION_STATE_LABELS[self.state]
        turn = self.transcript.active_turn
        if turn is not None and turn.stopping:
            return label_for_working_state(None, stopping=True)
        if turn is not None and turn.pending_prompts():
            return label_for_working_state("waiting_for_tool_confirmation")
        return label_for_working_state(turn.working_state if turn is not None else None)

    def live_settings(self) -> LiveSettings | None:
        """The settings the next turn runs with."""
        if not self.session_id:
            return None
        return LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode="auto")

    # ------------------------------------------------------------------ opening

    async def open_new(self) -> None:
        memory = self.manager.memory
        self.preface = memory.brief_for("devin", self.project) if memory is not None else ""
        await self._opened(self._start_session())

    async def open_saved(self) -> None:
        self._history_loaded = False
        await self._opened(self._resume_saved())

    async def _opened(self, opening: Any) -> None:
        self.state = SessionState.OPENING
        self._opening = asyncio.get_running_loop().create_future()
        self._notify("state")
        try:
            await opening
        finally:
            if not self._opening.done():
                self._opening.set_result(None)

    async def _start_session(self) -> None:
        client: Any = None
        session_id = ""
        try:
            client = await self.manager.connection()
            await self.manager.ensure_signed_in()
            result = await client.request(
                "session/new", {"cwd": str(self.project), "mcpServers": self.manager.memory_servers(self)}, timeout=NEW_SESSION_SECONDS
            )
            session_id = _text(result.get("sessionId")) if isinstance(result, Mapping) else ""
            if not session_id:
                raise DevinError("session/new returned no session id")
            self.session_id, self._client = session_id, client
            self.manager._attach(self)
            options = await self._configure(client, result.get("configOptions"))
            self._learn(options, adopt=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._close_requested:
                if client is not None and session_id:
                    await self._forget(client, session_id)
                return
            self.state = SessionState.CLOSED
            self.last_error = friendly_failure(error)
            self.transcript.add_error(self.last_error)
            self._notify("state")
            return
        if self._close_requested:
            await self._forget(client, session_id)
            return
        self.refresh_context()
        self.state = SessionState.READY
        self._notify("state")

    async def _resume_saved(self) -> None:
        try:
            client = await self.manager.connection()
            await self.manager.ensure_signed_in()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(friendly_failure(error))
            return
        result = await self._load(client, show=True)
        if result is None:
            return
        if self._close_requested:
            if self.session_id:
                await self._forget(client, self.session_id)
            return
        self.state = SessionState.READY
        self._notify("state")

    async def _load(self, client: Any, *, show: bool) -> Mapping[str, Any] | None:
        """``session/load``. ``show``: its replay becomes the transcript's history (otherwise it is only swallowed: the
        transcript has the history already). ``None``: it failed (the session is DEAD, with the reason)."""
        replay = Replay(self.transcript if show else Transcript())
        self._client = client
        self.manager._attach(self)
        self._replay = replay
        try:
            result = await client.request(
                "session/load",
                {"sessionId": self.session_id, "cwd": str(self.project), "mcpServers": self.manager.memory_servers(self)},
                timeout=LOAD_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(friendly_failure(error))
            return None
        finally:
            self._replay = None
        turns = replay.finish()
        result = result if isinstance(result, Mapping) else {}
        self._learn(result.get("configOptions"), adopt=show)
        if self._model:
            self.transcript.model = self._model
        if show:
            if turns:
                self.transcript.add_history_turns(turns)
            self._history_loaded = True
            for message_id, tokens in replay.usage.items():
                self._turn_usage[message_id] = tokens
            if replay.title and not self.transcript.title:
                self.transcript.title = replay.title
            if replay.context_used or replay.context_window:
                self._publish_context(
                    replace(
                        self.context,
                        used=replay.context_used or self.context.used,
                        window=replay.context_window or self.context.window,
                        model=self.model,
                    )
                )
        self.refresh_context()
        self._changed()
        return result

    async def _configure(self, client: Any, options: Any) -> Any:
        """Give a new session the settings asked for (what ``session/new`` answers is the account's defaults)."""
        # (Devin reports every change as it makes it, and what it reports is learned: what was asked for is taken first)
        wanted_model, wanted_level, wanted_mode = self.requested_model, self.reasoning, mode_of(self.autonomy)
        if wanted_model and wanted_model != current_value(options, "model"):
            try:
                options = await self._set_option(client, "model", wanted_model, options)
            except DevinRpcError as error:
                self.transcript.add_notice(f"Devin 不接受模型「{wanted_model}」（{error.message.strip() or error.code}），用默认模型。")
                self.requested_model = ""
        levels, level = thought_levels(options)
        if wanted_level and wanted_level in levels and wanted_level != level:
            with contextlib.suppress(DevinRpcError):
                options = await self._set_option(client, "thought_level", wanted_level, options)
        if wanted_mode != current_value(options, "mode"):
            try:
                await client.request("session/set_mode", {"sessionId": self.session_id, "modeId": wanted_mode}, timeout=CONFIG_SECONDS)
            except DevinRpcError as error:
                self.transcript.add_notice(f"Devin 不接受权限档位「{level_of(wanted_mode) or wanted_mode}」（{error.message.strip() or error.code}）。")
            else:
                options = [dict(o, currentValue=wanted_mode) if isinstance(o, Mapping) and o.get("id") == "mode" else o for o in options or ()]
        return options

    async def _set_option(self, client: Any, config_id: str, value: str, before: Any) -> Any:
        result = await client.request(
            "session/set_config_option", {"sessionId": self.session_id, "configId": config_id, "value": value}, timeout=CONFIG_SECONDS
        )
        after = result.get("configOptions") if isinstance(result, Mapping) else None
        return after if after else before

    def _learn(self, options: Any, *, adopt: bool = False) -> None:
        """Take what Devin says the session is set to. ``adopt``: it becomes what the user asked for, too."""
        if not options:
            return
        model = current_value(options, "model")
        if model:
            self._model = model
            if adopt:
                self.requested_model = model
            self.reasoning = thought_levels(options)[1]  # (choosing a model resets the level to the model's own)
        mode = level_of(current_value(options, "mode"))
        if mode:
            self.autonomy = mode

    async def _forget(self, client: Any, session_id: str) -> None:
        with contextlib.suppress(DevinError):
            await client.request("session/close", {"sessionId": session_id}, timeout=CLOSE_SECONDS)

    def _give_up(self, message: str) -> None:
        self.state = SessionState.DEAD
        self.last_error = message
        self.transcript.add_error(message, recoverable=True)
        self._notify("state")
        self._changed()

    def _abandon(self, error: BaseException) -> None:
        """Opening raised before a session existed (e.g. no Devin CLI): there is nothing to close."""
        self._close_requested = True
        if not isinstance(error, asyncio.CancelledError):
            self.last_error = friendly_failure(error)
        self.state = SessionState.CLOSED
        self._expire_pending()
        self._notify("state")
        self.manager._detach(self)
        self.manager._forget(self)

    # ------------------------------------------------------------------ closing and reconnecting

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
        client, session_id = self._client, self.session_id
        running = self._turn is not None
        self._expire_pending()
        if self.transcript.active_turn is not None:
            self.transcript.abort_turn("本轮已取消。")
        if self._turn is not None:
            self._end_turn(self._turn, quiet=True)
        if client is not None and client.running and session_id:
            if running:
                with contextlib.suppress(DevinError):
                    await client.notify("session/cancel", {"sessionId": session_id})
            await self._forget(client, session_id)
        self._client = None
        if self._background:
            current = asyncio.current_task()
            others = [task for task in self._background if task is not current]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        self.manager._detach(self)
        self.state = SessionState.CLOSED
        self._notify("state")
        self.manager._forget(self)

    def _connection_lost(self, error: BaseException) -> None:
        """The ``devin acp`` process is gone: DEAD, with a way back (``reconnect``)."""
        if self._close_requested or self.state in (SessionState.CLOSED, SessionState.DEAD):
            return
        self._client = None
        self.state = SessionState.DEAD
        self.last_error = friendly_failure(error)
        message = f"{self.last_error} 可以点击「重新连接」恢复会话。"
        turn = self._turn
        if turn is not None and self.transcript.active_turn is turn:
            self.transcript.fail_turn(message, recoverable=True)
        else:
            self.transcript.add_error(message, recoverable=True)
        self._expire_pending()
        if turn is not None:
            self._end_turn(turn, quiet=True)
        self._notify("state")
        self._changed()

    async def reconnect(self) -> None:
        """Load the same session in a new process after the old one died."""
        if self.state is not SessionState.DEAD or not self.session_id or self._close_requested:
            return
        self.transcript.resolve_recoverable_errors()
        self.transcript.add_notice("正在重新连接…")
        self.state = SessionState.OPENING
        self._notify("state")
        self._changed()
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
        try:
            client = await self.manager.connection()
            await self.manager.ensure_signed_in()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(f"重新连接失败：{friendly_failure(error)}")
            return
        if self._close_requested:
            return
        kept = (self.requested_model, self.reasoning, self.autonomy)
        loaded = await self._load(client, show=not self._history_loaded)
        if loaded is None:
            return
        # (the session is set the way it was before the process died, whatever the load reported)
        self.requested_model, self.reasoning, self.autonomy = kept
        with contextlib.suppress(DevinError):
            self._learn(await self._configure(client, loaded.get("configOptions")))
        if self._close_requested:
            return
        self.state = SessionState.READY
        self.transcript.add_notice("已重新连接，可以继续对话。")
        self._notify("state")
        self._changed()

    # ------------------------------------------------------------------ settings

    async def update_settings(
        self,
        *,
        model: str | None = None,
        reasoning: Any = None,
        autonomy: Any = None,
        mode: Any = None,
    ) -> bool:
        """Apply new settings now (``mode`` is ignored: Devin's own modes are the permission levels)."""
        client = self._client
        if not self.session_id or client is None or self.state not in (SessionState.READY, SessionState.RUNNING):
            return False
        try:
            if autonomy is not None:
                level = normalize_level(_enum_value(autonomy))
                if level is None:
                    self.transcript.add_notice(f"设置更新失败：Devin 没有「{_enum_value(autonomy)}」这个权限档位。")
                    self._changed()
                    return False
                await client.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode_of(level)}, timeout=CONFIG_SECONDS)
                self.autonomy = level
            if model:
                options = await self._set_option(client, "model", str(model), None)
                self.requested_model = self._model = str(model)
                self.transcript.model = self._model
                self._learn(options)
            if reasoning:
                wanted = _enum_value(reasoning)
                if wanted != self.reasoning:
                    options = await self._set_option(client, "thought_level", wanted, None)
                    self.reasoning = wanted
                    self._learn(options)
        except DevinProcessError as error:
            self._connection_lost(error)
            return False
        except DevinError as error:
            self.transcript.add_notice(f"设置更新失败：{friendly_failure(error)}")
            self._changed()
            return False
        self._changed()
        self._notify("settings")
        self.refresh_context()
        return True

    # ------------------------------------------------------------------ turns

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str | None:
        """Why these attachments cannot go to Devin (``None``: nothing known against them).

        Pictures go inline and text files as text blocks after the message; a PDF only by its extracted text."""
        if any(item.kind == IMAGE for item in attachments):
            info = self.manager.model_info(self.model)
            if info is not None and not info.supports_images:
                return f"当前模型「{info.display_name}」不支持图片输入：请先移除图片，或换一个支持图片的模型。"
        for item in attachments:
            if item.kind == PDF and not (item.parsed_text or "").strip():
                return f"Devin 只能读 PDF 里的文字，「{item.name}」里没有提取到文字：请换成图片或文本文件再发送。"
        return None

    def send_prompt(self, text: str, attachments: Sequence[Attachment] = ()) -> bool:
        """Start a turn. With attachments the text may be empty (``DEFAULT_PROMPT`` is sent instead)."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        if not text or self.state is not SessionState.READY or not self.session_id or self._client is None:
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
        self._turn = turn
        self._turn_done = asyncio.Event()
        self._outstanding, self._stop = 0, None
        self._calls.clear()
        self._block = ""
        self.transcript.set_working_state("thinking")
        self._spawn(self._run_prompt(turn, sent, outgoing, None))
        self._notify("state")
        self._changed()
        return True

    async def _run_prompt(self, turn: Turn, sent: str, attachments: Sequence[Attachment], steer: UserBlock | None) -> None:
        """One ``session/prompt``: it is answered when the turn is over (all prompts sent to a turn are answered together)."""
        client = self._client
        self._outstanding += 1
        failure: BaseException | None = None
        result: Any = None
        try:
            if client is None or not client.running:
                raise DevinProcessError("devin acp is not running")
            result = await client.request(
                "session/prompt", {"sessionId": self.session_id, "prompt": items.prompt_blocks(sent, attachments)}, timeout=None
            )
        except asyncio.CancelledError:
            if self._turn is turn:
                self.transcript.abort_turn("本轮已取消。")
                self._end_turn(turn)
            raise
        except Exception as error:  # the session must survive anything
            failure = error
        if self._turn is not turn:
            return  # (a stop that was not honoured in time ended the turn here already)
        self._outstanding -= 1
        if failure is not None:
            self._prompt_failed(turn, failure, steer)
            return
        if self._stop is None or not steer:
            self._stop = result if isinstance(result, Mapping) else {}
        if self._outstanding <= 0:
            self._complete(turn)

    def _prompt_failed(self, turn: Turn, error: BaseException, steer: UserBlock | None) -> None:
        if isinstance(error, DevinProcessError):
            self._connection_lost(error)
            return
        if steer is not None:
            # The turn goes on without what was said to it: the words go back to the user.
            self.transcript.remove_steer(turn, steer)
            self._unadopted.append((steer.text, tuple(steer.attachments)))
            self.transcript.add_notice(f"引导没有送达：{friendly_failure(error)}")
            self._notify("steer")
            self._changed()
            if self._outstanding <= 0:
                self._complete(turn)
            return
        if isinstance(error, ValueError):
            message = f"附件无法发送：{error}。"
        elif isinstance(error, (DevinError,)):
            message = friendly_failure(error)
        else:
            log.error("a Devin prompt failed", exc_info=error)
            message = f"出现内部错误：{type(error).__name__}: {error}"
        self.transcript.fail_turn(message)
        self._end_turn(turn)

    def _complete(self, turn: Turn) -> None:
        """Every prompt of the turn has been answered."""
        reason = _text(self._stop.get("stopReason")) if self._stop else "end_turn"
        note = STOP_NOTES.get(reason)
        if note:
            self.transcript.add_notice(note)
        # (a turn that was stopped reports no figures of its own: what its model calls said is all there is)
        self.transcript.finish_turn(turn, TurnStatus.INTERRUPTED if reason == "cancelled" else TurnStatus.DONE)
        self._end_turn(turn)
        self.refresh_context()

    def _end_turn(self, turn: Turn, *, quiet: bool = False) -> None:
        """The running turn is over (finished, failed, stopped or abandoned): back to READY."""
        if self._turn is not turn:
            return
        self._settle_expired()
        if self._handoff_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.handoff = None  # the session has the earlier text now; a failed turn keeps it for the retry
            self._handoff_turn = None
        if self._preface_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.preface = ""  # (likewise: it is in the session now)
            self._preface_turn = None
        if self._recall_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)
            self._recall_turn, self._recall_ids = None, frozenset()
        self._turn = None
        self._outstanding, self._stop = 0, None
        self._calls.clear()
        self._outputs.clear()
        self._exits.clear()
        self._started.clear()
        self._block = ""
        if self._turn_done is not None:
            self._turn_done.set()
        self.active_at = time.time()
        if self.state is SessionState.RUNNING:
            self.state = SessionState.READY
        if not quiet:
            self._notify("state")
            self._changed()

    async def steer(self, text: str, attachments: Sequence[Attachment] = ()) -> SteerOutcome:
        """Say something to the turn that is running, now: a prompt sent mid-turn is merged into that turn and the model
        reads it at its next step. It is shown at once; it is answered together with the turn."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        turn = self._turn
        if self.state is not SessionState.RUNNING or turn is None or self._client is None or not self.session_id:
            return SteerOutcome(False, over=True)
        if not text:
            return SteerOutcome(False, message="没有内容")
        if turn.stopping:
            return SteerOutcome(False, message="本轮正在停止")
        try:
            items.prompt_blocks(text, attachments)
        except ValueError as error:
            return SteerOutcome(False, message=f"附件无法发送：{error}")
        block = self.transcript.add_steer(turn, text, attachments, pending=False)
        self._changed()
        self._spawn(self._run_prompt(turn, text, attachments, block))
        return SteerOutcome(True)

    def take_unadopted(self) -> list[tuple[str, tuple[Attachment, ...]]]:
        """Words said to a running turn that Devin did not take (the prompt was refused): they go back to the user."""
        taken, self._unadopted = self._unadopted, []
        return taken

    async def interrupt(self) -> None:
        turn, client = self._turn, self._client
        if self.state is not SessionState.RUNNING or turn is None:
            return
        self.transcript.mark_stopping()
        self._changed()
        self._expire_pending()  # a question waiting for the user is answered "cancelled" and the turn can end
        if client is not None and self.session_id:
            try:
                await client.notify("session/cancel", {"sessionId": self.session_id})
            except DevinProcessError as error:
                self._connection_lost(error)
                return
            except DevinError as error:
                self.transcript.clear_stopping()
                self.transcript.add_notice(f"停止失败：{friendly_failure(error)}")
                self._changed()
                return
        done = self._turn_done
        if done is not None and self._turn is turn:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), INTERRUPT_GRACE_SECONDS)
        if self._turn is turn:
            log.warning("Devin did not end the turn within %.0f s of the stop request", INTERRUPT_GRACE_SECONDS)
            self.transcript.abort_turn("停止超时：Devin 没有及时结束这一轮，这里先停止显示。")
            self._end_turn(turn)

    # ------------------------------------------------------------------ context

    def refresh_context(self) -> None:
        """Devin pushes its figures (``usage_update``); this only keeps the window in step with the model."""
        model = self.model
        known = self.manager.model_window(model)
        window = known or (self.context.window if self.context.model in ("", model) else None)
        if window != self.context.window or model != self.context.model:
            self._publish_context(replace(self.context, window=window, model=model))

    def _publish_context(self, state: ContextState) -> None:
        if state != self.context:
            self.context = state
            self._notify("context")

    # ------------------------------------------------------------------ notifications

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        replay = self._replay
        if replay is not None:
            replay.feed(method, params)
            return
        try:
            if method == "session/update":
                update = params.get("update")
                if isinstance(update, Mapping):
                    self._on_update(update)
            elif method == "_cognition.ai/thinking_complete":
                self._on_thinking_complete(params)
            elif method == "_cognition.ai/turn_stats":
                self._on_stats(params)
        except Exception:
            log.exception("handling %s for session %s failed", method, self.session_id)

    def _live(self) -> Turn | None:
        turn = self._turn
        return turn if turn is not None and turn.status is TurnStatus.RUNNING else None

    def _on_update(self, update: Mapping[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._on_words("text", update)
        elif kind == "agent_thought_chunk":
            self._on_words("thinking", update)
        elif kind == "tool_call":
            self._on_tool_call(update)
        elif kind == "tool_call_update":
            self._on_tool_update(update)
        elif kind == "session_info_update":
            title = _text(update.get("title")).strip()
            if title and title != self.transcript.title:
                self.transcript.title = title
                self._changed()
        elif kind == "usage_update":
            self._on_usage(update)
        elif kind == "config_option_update":
            before = (self._model, self.reasoning, self.autonomy)
            self._learn(update.get("configOptions"))
            if (self._model, self.reasoning, self.autonomy) != before:
                self.transcript.model = self._model or self.transcript.model
                self._changed()
                self._notify("settings")
        elif kind == "current_mode_update":
            level = level_of(update.get("currentModeId"))
            if level and level != self.autonomy:
                self.autonomy = level
                self._changed()
                self._notify("settings")

    def _switch(self, turn: Turn, to: str) -> None:
        """What the turn writes next is of another kind than the last: the last block is complete."""
        if self._block == to:
            return
        key = f"devin-{self._block}-{self._blocks}"
        if self._block == "text":
            self.transcript.end_text(turn, key, 0)
        elif self._block == "thinking":
            self.transcript.end_thinking(turn, key, 0)
        if to in ("text", "thinking"):
            self._blocks += 1
        self._block = to

    def _on_words(self, kind: str, update: Mapping[str, Any]) -> None:
        turn = self._live()
        content = update.get("content")
        text = _text(content.get("text")) if isinstance(content, Mapping) else ""
        if turn is None or not text:
            return
        self._switch(turn, kind)
        key = f"devin-{kind}-{self._blocks}"
        if kind == "text":
            self.transcript.append_text(turn, key, 0, text)
            self.transcript.set_working_state("streaming_assistant_message")
        else:
            self.transcript.append_thinking(turn, key, 0, text)
            self.transcript.set_working_state("thinking")
        self._changed()

    def _on_thinking_complete(self, params: Mapping[str, Any]) -> None:
        turn = self._live()
        if turn is None or self._block != "thinking":
            return
        milliseconds = params.get("durationMs")
        duration = milliseconds / 1000.0 if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool) else None
        self.transcript.end_thinking(turn, f"devin-thinking-{self._blocks}", 0, duration=duration)
        self._block = ""
        self._changed()

    def _prompting(self, turn: Turn, call_id: str) -> bool:
        return any(prompt.status is PromptStatus.PENDING and call_id in getattr(prompt, "tool_use_ids", ()) for prompt in turn.prompts)

    def _on_tool_call(self, update: Mapping[str, Any]) -> None:
        turn, call_id = self._live(), _text(update.get("toolCallId"))
        if turn is None or not call_id:
            return
        self._switch(turn, "tool")
        name, data = items.tool_spec(update)
        tool = self.transcript.ensure_tool(turn, call_id, name)
        self.transcript.set_tool_input(turn, tool, data, final=True)
        diffs = items.call_diffs(update.get("content"))
        if diffs:
            self.transcript.set_tool_diffs(turn, tool, diffs)
        if not self._prompting(turn, call_id):
            self.transcript.tool_running(turn, tool)
        self.transcript.set_working_state("executing_tool")
        self._changed()

    def _on_tool_update(self, update: Mapping[str, Any]) -> None:
        turn, call_id = self._live(), _text(update.get("toolCallId"))
        if turn is None or not call_id:
            return
        tool = turn.tools.get(call_id)
        if tool is None:  # (an update for a call that was never announced)
            name, data = items.tool_spec(update)
            tool = self.transcript.ensure_tool(turn, call_id, name)
            self.transcript.set_tool_input(turn, tool, data, final=False)
        shown = items.content_text([c for c in update.get("content") or () if not (isinstance(c, Mapping) and c.get("type") == "diff")])
        if shown:
            # (the output arrives as ever longer snapshots of the whole text: the latest one replaces the rest)
            self._outputs[call_id] = shown[-OUTPUT_KEEP_CHARS:]
            if not self._prompting(turn, call_id):
                self.transcript.tool_running(turn, tool, self._outputs[call_id])
        diffs = items.call_diffs(update.get("content"))
        if diffs:
            self.transcript.set_tool_diffs(turn, tool, diffs)
        meta = update.get("_meta") if isinstance(update.get("_meta"), Mapping) else {}
        exit_info = meta.get("terminal_exit")
        code = exit_info.get("exit_code") if isinstance(exit_info, Mapping) else None
        if isinstance(code, int) and not isinstance(code, bool):
            self._exits[call_id] = code
        status = items.call_status(update.get("status"))
        if status is not None:
            result = self._outputs.pop(call_id, "")
            if tool.name == items.EXECUTE and call_id in self._exits:
                result = command_result({"exitCode": self._exits.pop(call_id)}, result)
            self.transcript.finish_tool(turn, tool, result, status=status)
            self.transcript.set_working_state("thinking")
        elif update.get("status") == "in_progress" and not self._prompting(turn, call_id):
            self.transcript.tool_running(turn, tool)
        self._changed()

    def _on_usage(self, update: Mapping[str, Any]) -> None:
        meta = update.get("_meta") if isinstance(update.get("_meta"), Mapping) else {}
        if items.SUBAGENT_META in meta:
            return  # (each model call is reported twice, the second time as the root agent's: the first is the one)
        tokens, used, size = items.usage_call(update)
        if tokens is not None and self._turn is not None:
            self._calls.append(tokens)
        if size:
            self.manager._learn_window(self.model, size)
        context = self.context
        self._publish_context(
            replace(context, used=used if used is not None else context.used, window=size or context.window, model=self.model, compacted=False)
        )
        self._changed()

    def _on_stats(self, params: Mapping[str, Any]) -> None:
        message_id = _text(params.get("turnClientMessageId"))
        if not message_id:
            return
        tokens = items.stats_tokens(params)
        if tokens is None and self._calls:
            tokens = self._calls[0]
            for more in self._calls[1:]:
                tokens = tokens.plus(more)
        self._calls.clear()  # (covered by this figure now: they must not stay in what is shown as still counting)
        if tokens is not None:
            self._turn_usage[message_id] = tokens
            self._changed()

    # ------------------------------------------------------------------ requests (approvals)

    async def _on_server_request(self, method: str, params: Mapping[str, Any], request_id: Any) -> Any:
        if method == "session/request_permission":
            return await self._permission(params, request_id)
        if method == "elicitation/create":
            return await self._elicitation(params, request_id)
        raise DevinRpcError(METHOD_NOT_FOUND, f"WittyCode does not handle {method}")

    async def _elicitation(self, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        """Devin asks the user something (its ``ask_user_question``): ACP's ``elicitation/create``, the MCP data model (a
        ``form`` with a flat schema, or a ``url`` to open). Shown with the question cards; answered ``accept`` (with the
        values), ``decline`` or ``cancel``. What cannot be shown is declined, and the user is told."""
        plan = plan_of({**params, "serverName": "Devin"})
        if plan is None:
            self.transcript.add_notice(f"Devin 发来一种 WittyCode 不会显示的提问（{_text(params.get('mode')) or '未知'}），已拒绝。")
            self._changed()
            return {"action": "decline"}
        turn = self._live()
        if turn is None:
            return {"action": "decline"}
        # (a question tied to one of Devin's tool calls sits on that call's row, and Devin reports how the call ended)
        call_id = _text(params.get("toolCallId"))
        item_id = call_id or f"elicitation-{request_id}"
        prompt = self.transcript.add_question_prompt(item_id, plan.views)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return {"action": "cancel"}
        value = await self._wait(prompt.id, request_id)
        reply = {key: value for key, value in reply_for(plan, value).items() if value is not None}
        if plan.mode == "url" and reply["action"] == "accept":
            self.manager.open_external(plan.url)
        tool = turn.tools.get(item_id)
        if tool is not None and not call_id and not tool.terminal:
            done = {"accept": "已回答。", "decline": "已拒绝。", "cancel": "已跳过。"}[reply["action"]]
            self.transcript.finish_tool(turn, tool, done, status=ToolStatus.DONE if reply["action"] == "accept" else ToolStatus.CANCELLED)
            self._changed()
        return reply

    def _action(self, call_id: str, call: Mapping[str, Any], tool: ToolItem) -> ActionView:
        meta = call.get("_meta") if isinstance(call.get("_meta"), Mapping) else {}
        rule = _text(meta.get("cognition.ai/permissionRule"))
        path = _text(tool.input.get("file_path")) or (tool.diffs[0].path if tool.diffs else "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""
        if tool.name == items.EXECUTE:
            command = _text(meta.get("cognition.ai/editableCommand")) or _text(tool.input.get("command"))
            return ActionView(kind="execute", tool_use_id=call_id, tool_name=tool.name, title="运行命令", command=command, reason=rule or None)
        if tool.name == "Create":
            return ActionView(kind="create", tool_use_id=call_id, tool_name=tool.name, title=f"创建文件 {name}", path=path or None, diffs=tool.diffs, reason=rule or None)
        if tool.name == "Edit":
            return ActionView(kind="edit", tool_use_id=call_id, tool_name=tool.name, title=f"编辑文件 {name}", path=path or None, diffs=tool.diffs, reason=rule or None)
        return ActionView(kind="other", tool_use_id=call_id, tool_name=tool.name, title=f"使用工具 {tool.name}", path=path or None, diffs=tool.diffs, reason=rule or None)

    async def _permission(self, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        turn = self._live()
        call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
        call_id = _text(call.get("toolCallId"))
        options = [option for option in params.get("options") or () if isinstance(option, Mapping)]
        if turn is None or not call_id:
            return outcome_for("cancel", options)
        tool = turn.tools.get(call_id)
        if tool is None:
            name, data = items.tool_spec(call)
            tool = self.transcript.ensure_tool(turn, call_id, name)
            if not tool.input_final and data:
                self.transcript.set_tool_input(turn, tool, data, final=False)
        prompt = self.transcript.add_permission_prompt([call_id], [self._action(call_id, call, tool)], option_views(options))
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return CANCELLED
        self._options[prompt.id] = options
        try:
            value = await self._wait(prompt.id, request_id)
        finally:
            self._options.pop(prompt.id, None)
        return outcome_for(value, options)

    async def _wait(self, prompt_id: str, request_id: Any) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[prompt_id] = future
        self._requests[request_id] = prompt_id
        try:
            return await future
        except asyncio.CancelledError:
            self._expire_prompt(prompt_id)
            raise
        finally:
            self._pending.pop(prompt_id, None)
            if self._requests.get(request_id) == prompt_id:
                del self._requests[request_id]
            self._changed()

    def answer_permission(self, prompt_id: str, value: str) -> bool:
        """Answer a pending approval; ``False`` when it is no longer open (late click)."""
        future = self._pending.get(prompt_id)
        if future is None or not self.transcript.answer_permission(prompt_id, value):
            return False
        if not future.done():
            future.set_result(value)
        return True

    def answer_question(self, prompt_id: str, answers: Sequence[str | Sequence[str]] | None) -> bool:
        """Answer (or, with ``None``, skip) a question Devin asked (``elicitation/create``); each answer is a string or a list."""
        future = self._pending.get(prompt_id)
        if future is None:
            return False
        texts = None if answers is None else [answer_text(answer) for answer in answers]
        if not self.transcript.answer_question(prompt_id, texts):
            return False
        if not future.done():
            future.set_result(None if answers is None else [[answer] if isinstance(answer, str) else list(answer) for answer in answers])
        return True

    def _expire_prompt(self, prompt_id: str) -> None:
        self.transcript.expire_prompt(prompt_id)
        future = self._pending.pop(prompt_id, None)
        if future is not None and not future.done():
            future.set_result(None)  # answered "cancelled": the request handler returns to Devin normally

    def _expire_pending(self) -> None:
        for prompt_id in list(self._pending):
            self._expire_prompt(prompt_id)
        self.transcript.expire_prompts()

    def _settle_expired(self) -> None:
        # The turn ended or the process died while a prompt was open: release the waiting handler.
        for prompt_id, future in list(self._pending.items()):
            prompt = self.transcript.prompt(prompt_id)
            if prompt is None or prompt.status is not PromptStatus.PENDING:
                self._pending.pop(prompt_id, None)
                if not future.done():
                    future.set_result(None)


__all__ = ["DevinController"]
