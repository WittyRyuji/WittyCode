"""One ChatGPT Plan conversation as an open session, with the public surface of ``SessionController`` (``core/session_manager``)
and of the Codex and Devin controllers, so the backend treats all engines alike.

Unlike those, nothing here is an engine process: the conversation is a list of Responses items (``saved.items``) and each
message starts a turn of WittyCode's own agent loop (``harness.loop``) that posts to OpenAI (``engine.stream``) and runs the
tools itself (``harness.toolset``). The controller is the loop's observer: it turns what the loop reports into the
transcript the page shows, asks the user about the actions the permission level does not allow, and saves the conversation
(``harness.store``) while it goes.

Lifecycle (``SessionState``): ``open_new`` / ``open_saved`` -> READY at once (the shared memory's server starts in the
background; the first turn waits for it, briefly) -> ``send_prompt``: RUNNING while the loop runs -> READY. ``interrupt``
cancels the loop's task (the connection is dropped, a running command is killed, every call still gets its output) and the
turn ends as stopped. ``close`` stops the turn, the servers, and saves.

Messages typed while a turn runs are ``steer``: shown at once as waiting, given to the model at its next request; the turn
that ends before the loop took one hands it back (``take_unadopted``). Token figures: ``usage_series`` is one cumulative
series for the whole conversation (kept in the file), so reopening it adds nothing twice.

Saving: a turn is on disk as ``running`` until it ends, and what is written is always a history the next request could send
(a call still waiting or running has ``store.LOST`` there). A call that may change something is preceded by a save, so a
crash while it runs leaves a file that knows it was made. ``open_saved`` repairs a file the application did not live to
finish (``store.recover``) and saves the result; nothing is run again.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from desktop.core.attachments import DEFAULT_PROMPT, IMAGE, PDF, TEXT, Attachment, sniff_image
from desktop.core.chatgpt import history as saved_history
from desktop.core.chatgpt.errors import AuthError, ChatGPTError, friendly_failure
from desktop.core.chatgpt.responses import Usage
from desktop.core.engine_items import attachment_block
from desktop.core.connections import ConnectionError as ExtensionError
from desktop.core.context import ContextState, format_tokens
from desktop.core.context_envelope import wrap as wrap_context
from desktop.core.handoff import Handoff
from desktop.core.harness import asking, compact, skills
from desktop.core.harness.loop import INTERRUPTED, AgentLoop, Call, Observer, Outcome, Steer, Verdict
from desktop.core.harness.mcp import McpClient, McpError
from desktop.core.harness.permissions import LEVELS
from desktop.core.harness.prompt import build_instructions
from desktop.core.harness.store import SavedSession, SavedTurn, diff_to_json, pair_in_place, paired, recover
from desktop.core.harness.tools import ToolContext
from desktop.core.harness.toolset import HarnessTools
from desktop.core.hooks import PromptEvent
from desktop.core.memory.launch import new_run
from desktop.core.prompts import APPROVAL_OPTIONS, QuestionView
from desktop.core.session_state import SESSION_STATE_LABELS, LiveSettings, SessionState, SteerOutcome
from desktop.core.toolinfo import working_label as label_for_working_state
from desktop.core.transcript import PromptStatus, ToolStatus, Transcript, Turn, TurnStatus, UserBlock
from desktop.core.usage import Tokens, own_output

if TYPE_CHECKING:
    from desktop.core.chatgpt.engine import ChatGPTEngine

log = logging.getLogger("desktop.chatgpt")

DEFAULT_LEVEL = "code"
INTERRUPT_GRACE_SECONDS = 8.0
MCP_WAIT_SECONDS = 20.0
SAVE_DELAY_SECONDS = 2.0
STOP_NOTES = {
    "max_output_tokens": "回答太长，被截断了；可以回复「继续」。",
    "content_filter": "回答被内容过滤停止了。",
}
CONTEXT_FULL = "context_full"
"""The code of the failure that ends a turn whose next request cannot be made to fit the model's window."""
MANUAL_WINDOW = 128_000
"""The window a compaction the user asked for is measured against when the model's is not known."""
MODES = ("auto", "spec")
PLAN = "spec"
"""Droid's own word for what the page calls Plan."""


def normalize_level(value: Any) -> str | None:
    text = str(getattr(value, "value", value) or "")
    return text if text in LEVELS else None


def user_item(text: str, attachments: Sequence[Attachment] = ()) -> dict[str, Any]:
    """The user's message as a Responses item: the typed text, then each file's text as a block of its own, then pictures."""
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    images: list[dict[str, Any]] = []
    for attachment in attachments:
        if attachment.kind == IMAGE and attachment.data:
            media_type = attachment.media_type or sniff_image(attachment.data) or "image/png"
            images.append({"type": "input_image", "image_url": f"data:{media_type};base64,{base64.b64encode(attachment.data).decode('ascii')}"})
        elif attachment.kind == TEXT:
            content.append({"type": "input_text", "text": attachment_block(attachment.name, "text/plain", attachment.text)})
        elif attachment.kind == PDF:
            if not (attachment.parsed_text or "").strip():
                raise ValueError(f"PDF「{attachment.name}」里没有可读的文字")
            content.append({"type": "input_text", "text": attachment_block(attachment.name, "application/pdf", attachment.parsed_text or "")})
    return {"type": "message", "role": "user", "content": content + images}


@dataclass(eq=False)
class ChatGPTController:
    """Owns one conversation: its transcript, the running turn and the pending approvals."""

    manager: ChatGPTEngine
    project: Path
    transcript: Transcript = field(default_factory=Transcript)
    state: SessionState = SessionState.OPENING
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    saved_title: str | None = None
    opened_at: float = field(default_factory=time.time)
    used_at: float = field(default_factory=time.time)
    active_at: float = field(default_factory=time.time)
    """When the conversation last had a message sent or answered (the sidebar's order and time)."""
    last_error: str = ""
    eviction_notice: str = ""
    context: ContextState = field(default_factory=ContextState)
    handoff: Handoff | None = None
    requested_model: str = ""
    reasoning: str = ""
    autonomy: str = DEFAULT_LEVEL
    """``ask`` | ``code`` | ``full`` (``harness.permissions``)."""
    mode: str = "auto"
    """``auto`` | ``spec`` (Plan: ``HarnessTools.plan``). A change reaches the next turn, not the one that runs."""
    saved_settings: LiveSettings | None = None
    engine: ClassVar[str] = "chatgpt"
    run: str = field(default_factory=new_run)
    """Tells the shared memory which conversation a note comes from (it is in the command of the memory server)."""
    preface: str = ""
    recalled: set[str] = field(default_factory=set)
    saved: SavedSession = field(init=False)
    _usage_total: Tokens | None = None
    _usage_models: dict[str, Tokens] = field(default_factory=dict)
    _connection_started: bool = False
    _connection_context: str = ""
    _turn: Turn | None = None
    _turn_task: asyncio.Task[Any] | None = None
    _turn_done: asyncio.Event | None = None
    _turn_start: int = 0
    _turn_model: str = ""
    _turn_reasoning: str = ""
    _turn_mode: str = ""
    """What the running turn asks of the model. Fixed when it starts: the request, the summary, the window it is measured against
    and the figure on the page all belong to it, so a model chosen meanwhile cannot split them (it is the next turn's)."""
    _measure: tuple[str, tuple[Any, ...], int, int] | None = None
    """``(model, record, length, tokens)`` of the last response of the last turn: the size of the first ``length`` items of the
    conversation's view as the model counted it, valid while the model and the compaction record are the same."""
    _prompt_notes: tuple[str, ...] = ()
    """What the conversation last told the user about the instruction files (told again only when it changes)."""
    _handoff_turn: Turn | None = None
    _preface_turn: Turn | None = None
    _recall_turn: Turn | None = None
    _recall_ids: frozenset[str] = frozenset()
    _steers: list[Steer] = field(default_factory=list)
    _unadopted: list[tuple[str, tuple[Attachment, ...]]] = field(default_factory=list)
    _clients: list[McpClient] = field(default_factory=list)
    _mcp_task: asyncio.Task[None] | None = None
    _tools: HarnessTools | None = None
    _context: ToolContext | None = None
    _pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    _listeners: list[Callable[[ChatGPTController, str], None]] = field(default_factory=list)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _save_handle: asyncio.TimerHandle | None = None
    _close_requested: bool = False
    _close_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.saved = SavedSession(id=self.session_id, project=str(self.project))

    # ------------------------------------------------------------------ plumbing

    def add_listener(self, callback: Callable[[ChatGPTController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ChatGPTController, str], None]) -> None:
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
        stored = self.saved.title
        if stored and stored != "新会话":
            # (a saved conversation keeps the name it was saved under: its first message is not its title, and an
            # imported one is named after the conversation it came from)
            return stored
        first = next((t for t in self.transcript.turns if t.user_block), None)
        if first is not None and first.user_block is not None:
            lines = first.user_block.text.strip().splitlines()
            text = lines[0] if lines else ""
            if text:
                return text[:60]
        return self.saved_title or "新会话"

    @property
    def model(self) -> str:
        return self.requested_model or self.transcript.model or ""

    @property
    def running_model(self) -> str:
        """The model whose window and answers the conversation is measured by now: the running turn's own, else the chosen one."""
        return self._turn_model or self.model

    @property
    def usage(self) -> Tokens | None:
        return self._usage_total

    def usage_series(self) -> dict[str, Tokens]:
        """One cumulative series for the whole conversation: a figure never goes down, and the file keeps it, so a reopened
        conversation reports where it was and adds nothing twice."""
        return {"self": self._usage_total} if self._usage_total is not None else {}

    def usage_model_series(self) -> dict[str, tuple[str, Tokens]]:
        return {f"model:{model}": (model, tokens) for model, tokens in self._usage_models.items()}

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
        if self.state is SessionState.CLOSED:
            return None
        return LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode=self.mode)

    # ------------------------------------------------------------------ opening

    async def open_new(self) -> None:
        memory = self.manager.memory
        self.preface = memory.brief_for("chatgpt", self.project) if memory is not None else ""
        self._ready()

    async def open_saved(self) -> None:
        self._notify("state")  # announce the opening tab so its cached history can be shown before reading the archive
        saved = await asyncio.to_thread(self.manager.sessions.load, self.session_id)
        if saved is None:
            raise ChatGPTError("这个会话的存档找不到了。", code="missing_session")
        recovered = recover(saved)
        self.saved = saved
        self.requested_model = saved.model
        self.reasoning = saved.reasoning
        self.autonomy = normalize_level(saved.autonomy) or DEFAULT_LEVEL
        self.mode = PLAN if saved.mode == PLAN else "auto"
        self.saved_title = saved.title or self.saved_title
        self.transcript.model = saved.model or None
        self.transcript.add_history_turns(saved_history.build_turns(self.transcript, self.manager.registry, saved))
        usage = saved.usage
        if usage:
            self._usage_total = Tokens(usage.get("input", 0), usage.get("output", 0), None, usage.get("cacheRead", 0))
        self._usage_models = {model: tokens for model, raw in saved.usage_models.items() if (tokens := Tokens.of(raw)) is not None}
        self.context = ContextState(used=saved.context_used or None, window=self.manager.model_window(self.model), model=self.model)
        self.saved_settings = LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode=self.mode)
        if recovered:
            log.warning("the ChatGPT conversation %s was saved in the middle of a turn (the application closed or crashed); repaired", self.session_id)
            try:
                self.manager.sessions.save(saved)
            except OSError:
                log.warning("could not save the repaired conversation", exc_info=True)
        self._ready()

    def _ready(self) -> None:
        self._mcp_task = self._spawn(self._start_servers())
        self.state = SessionState.READY
        self.refresh_context()
        self._notify("state")
        self._changed()

    async def _start_servers(self) -> None:
        """The shared memory's server. Without it the conversation works the same, without the memory."""
        for command in self.manager.memory_servers(self):
            client = McpClient(command.name, command.command, command.args, command.env, cwd=str(self.project))
            try:
                await client.start()
            except McpError as error:
                log.warning("the shared memory could not be started for a ChatGPT conversation: %s", error)
                continue
            if self._close_requested:
                await client.close()
                return
            self._clients.append(client)

    async def _ensure_tools(self) -> HarnessTools:
        if self._mcp_task is not None and not self._mcp_task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._mcp_task), MCP_WAIT_SECONDS)
        self._context = self._context or ToolContext(self.project, threading.Event(), protected=(self.manager.root,))
        if self._tools is None:
            self._tools = HarnessTools(self.manager.registry, self._context, self.autonomy, self._clients, asker=self._ask_questions)
        else:
            self._tools.clients = list(self._clients)
            self._tools.refresh()
        self._tools.level = self.autonomy
        return self._tools

    def _abandon(self, error: BaseException) -> None:
        self.state = SessionState.CLOSED
        self.last_error = friendly_failure(error) if isinstance(error, ChatGPTError) else f"{type(error).__name__}: {error}"
        self.manager._forget(self)
        self._notify("state")

    # ------------------------------------------------------------------ closing

    async def close(self) -> None:
        if self._close_task is None:
            self._close_requested = True
            self._close_task = asyncio.ensure_future(self._do_close())
        await asyncio.shield(self._close_task)

    async def _do_close(self) -> None:
        task = self._turn_task
        if task is not None and not task.done():
            self._expire_pending()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), INTERRUPT_GRACE_SECONDS)
        if self._mcp_task is not None and not self._mcp_task.done():
            self._mcp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._mcp_task
        for client in self._clients:
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        connections = getattr(self.manager, "connections", None)
        if connections is not None and self._connection_started:
            with contextlib.suppress(Exception):
                await connections.hooks("SessionEnd", self.project, {"session_id": self.session_id, "reason": "other"})
        self._save_now()
        self.state = SessionState.CLOSED
        self.manager._forget(self)
        self._notify("state")

    async def reconnect(self) -> None:
        """There is no process to lose; a conversation that was marked dead simply goes on."""
        if self.state is SessionState.DEAD:
            self.state = SessionState.READY
            self._notify("state")
            self._changed()

    # ------------------------------------------------------------------ settings

    async def update_settings(self, *, model: str | None = None, reasoning: Any = None, autonomy: Any = None, mode: Any = None) -> bool:
        if self.state not in (SessionState.READY, SessionState.RUNNING):
            return False
        if autonomy is not None:
            level = normalize_level(autonomy)
            if level is None:
                self.transcript.add_notice(f"设置更新失败：没有「{getattr(autonomy, 'value', autonomy)}」这个权限档位。")
                self._changed()
                return False
            self.autonomy = level
            if self._tools is not None:
                self._tools.level = level
        if mode is not None:
            chosen = str(getattr(mode, "value", mode))
            if chosen not in MODES:
                self.transcript.add_notice(f"设置更新失败：没有「{chosen}」这个模式。")
                self._changed()
                return False
            self.mode = chosen
        if model:
            self.requested_model = str(model)
            self.transcript.model = self.requested_model
        if reasoning:
            self.reasoning = str(getattr(reasoning, "value", reasoning))
        self._explain_pending()
        self._changed()
        self._notify("settings")
        self.refresh_context()
        self._save_soon()
        return True

    def _explain_pending(self) -> None:
        """A model or a reasoning level chosen while a turn runs is the next turn's: say so, so that the chip and the turn do not
        seem to disagree."""
        if self.state is not SessionState.RUNNING or self._turn is None or not self._turn_model:
            return
        changes: list[str] = []
        if self.model != self._turn_model:
            now, was = self.manager.model_info(self.model), self.manager.model_info(self._turn_model)
            changes.append(f"模型改为「{now.display_name if now else self.model}」（这一轮仍用「{was.display_name if was else self._turn_model}」）")
        if (self.reasoning or "") != self._turn_reasoning:
            changes.append(f"推理强度改为「{self.reasoning or '默认'}」（这一轮仍用「{self._turn_reasoning or '默认'}」）")
        if self.mode != self._turn_mode:
            changes.append(f"模式改为「{'Plan' if self.mode == PLAN else 'Auto'}」（这一轮仍是「{'Plan' if self._turn_mode == PLAN else 'Auto'}」）")
        if changes:
            self.transcript.add_notice("、".join(changes) + "，从下一条消息起生效。")

    # ------------------------------------------------------------------ turns

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str | None:
        if any(item.kind == IMAGE for item in attachments):
            info = self.manager.model_info(self.model)
            if info is not None and not info.supports_images:
                return f"当前模型「{info.display_name}」不支持图片输入：请先移除图片，或换一个支持图片的模型。"
        for item in attachments:
            if item.kind == PDF and not (item.parsed_text or "").strip():
                return f"ChatGPT 只能读 PDF 里的文字，「{item.name}」里没有提取到文字：请换成图片或文本文件再发送。"
        return None

    def send_prompt(self, text: str, attachments: Sequence[Attachment] = ()) -> bool:
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        if not text or self.state is not SessionState.READY:
            return False
        self.used_at = self.active_at = time.time()
        self.state = SessionState.RUNNING
        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode
        context = self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, frozenset(self.recalled), first=bool(self.preface)))
        sent, carried, outgoing = text, None, attachments
        handoff = self.handoff if self.handoff is not None and not self.handoff.empty else None
        if handoff is not None:
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
        self.transcript.set_working_state("thinking")
        self._turn_task = self._spawn(self._run_turn(turn, sent, outgoing))
        self._notify("state")
        self._changed()
        return True

    async def _run_turn(self, turn: Turn, sent: str, attachments: Sequence[Attachment]) -> None:
        failure: BaseException | None = None
        end = None
        loop: AgentLoop | None = None
        model, reasoning = self._turn_model or self.model, self._turn_reasoning if self._turn_model else self.reasoning
        plan = (self._turn_mode if self._turn_model else self.mode) == PLAN
        try:
            tools = await self._ensure_tools()
            tools.plan = plan
            tools.connections = getattr(self.manager, "connections", None)
            assert self._context is not None
            self._context.abort = threading.Event()
            # (read again every turn: a skill made during the conversation is there for the next message)
            found = await asyncio.to_thread(skills.discover, self.project)
            self._context.readable = skills.read_roots(found)
            if tools.connections is not None:
                if not self._connection_started:
                    self._connection_context = await tools.connections.hooks("SessionStart", self.project, {"session_id": self.session_id, "source": "resume" if self.saved.items else "startup"})
                    self._connection_started = True
                extra = await tools.connections.hooks("UserPromptSubmit", self.project, {"session_id": self.session_id, "prompt": turn.user_block.text if turn.user_block else sent})
                sent += wrap_context(self._connection_context, extra)
            item = user_item(sent, attachments)
            self._turn_start = len(self.saved.items)
            self.saved.items.append(item)
            self.saved.turns.append(SavedTurn(self._turn_start, time.time(), None, "running", "", model))
            self.saved.model, self.saved.reasoning, self.saved.autonomy = self.model, self.reasoning, self.autonomy
            self.saved.mode = PLAN if self.mode == PLAN else ""
            notes: list[str] = []
            instructions = build_instructions(
                self.project,
                model=model,
                level=self.autonomy,
                servers=tools.servers(),
                notes=notes,
                plan=plan,
                skills=found,
                think_chinese=self.manager.settings.think_chinese,
            )
            if tuple(notes) != self._prompt_notes:
                self._prompt_notes = tuple(notes)
                for note in notes:
                    self.transcript.add_notice(note)
            loop = AgentLoop(
                self.manager.stream,
                tools,
                _Watch(self, tools, turn, instructions=instructions),
                model=model,
                reasoning=reasoning or None,
                instructions=instructions,
                shape=self.manager.shape,
                conversation=self.session_id,
                max_steps=lambda: self.manager.settings.max_steps,
                **self._carried_measure(model),
            )
            # The loop works on the view (what the model reads: the summary of the early part, then the rest); what it adds
            # reaches the log (``saved.items``) through ``_Watch.history_grew``.
            end = await loop.run(compact.view_of(self.saved.items, self.saved.compaction))
            if tools.connections is not None:
                await tools.connections.hooks("Stop", self.project, {"session_id": self.session_id, "stop_hook_active": True})
        except asyncio.CancelledError:
            if self._turn is turn:
                self._release_steers()
                self.transcript.abort_turn("本轮已停止。")
                self._record_turn("interrupted")
                self._end_turn(turn)
            raise
        except Exception as error:  # the session must survive anything
            failure = error
        finally:
            if loop is not None and (self._turn is None or self._turn is turn):  # (not while a later turn runs: this loop was left behind)
                self._keep_measure(loop, model)
        if self._turn is not turn:
            return
        if failure is not None:
            self._turn_failed(turn, failure)
            return
        assert end is not None
        note = ""
        if end.reason == "incomplete":
            note = STOP_NOTES.get(end.detail, f"回答没有说完（{end.detail}）。")
        elif end.reason == "step_limit":
            note = f"这一轮用了 {end.steps} 步还没结束，先停在这里；可在设置 → 会话里调整「单轮步数上限」，或回复「继续」。"
        if note:
            self.transcript.add_notice(note)
        self._release_steers()
        self.transcript.finish_turn(turn, TurnStatus.DONE)
        self._record_turn({"step_limit": "limit", "incomplete": "incomplete"}.get(end.reason, "done"), note=note)
        self._end_turn(turn)
        self.refresh_context()

    def _turn_failed(self, turn: Turn, error: BaseException) -> None:
        if isinstance(error, ChatGPTError):
            message = friendly_failure(error)
            log.warning("a ChatGPT turn failed: %s", error)
            if isinstance(error, AuthError) and error.terminal:
                self.manager._emit_account()
        elif isinstance(error, ExtensionError):
            message = f"扩展连接未完成：{error}"
        elif isinstance(error, ValueError):
            message = f"附件无法发送：{error}。"
        else:
            log.error("a ChatGPT turn failed unexpectedly", exc_info=error)
            message = f"出现内部错误：{type(error).__name__}: {error}"
        if len(self.saved.items) == self._turn_start + 1 and self.saved.turns:
            # (nothing came of the turn: the message is not left in the history, the next try is not a repeat of it)
            del self.saved.items[self._turn_start :]
            self.saved.turns.pop()
        else:
            self._record_turn("failed", message)
        self._release_steers()
        self.transcript.fail_turn(message)
        self._end_turn(turn)

    def _record_turn(self, status: str, error: str = "", note: str = "") -> None:
        if self.saved.turns and self.saved.turns[-1].finished is None:
            record = self.saved.turns[-1]
            record.finished, record.status, record.error, record.note = time.time(), status, error, note

    def _end_turn(self, turn: Turn) -> None:
        if self._turn is not turn:
            return
        self._settle_expired()
        if self._handoff_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.handoff = None
            self._handoff_turn = None
        if self._preface_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.preface = ""
            self._preface_turn = None
        if self._recall_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.recalled |= self._recall_ids
            self._recall_turn, self._recall_ids = None, frozenset()
        if turn.status is TurnStatus.DONE:
            self._connection_context = ""
        self._turn = None
        self._turn_model = self._turn_reasoning = self._turn_mode = ""
        if self._turn_done is not None:
            self._turn_done.set()
        self.active_at = time.time()
        if self.state is SessionState.RUNNING:
            self.state = SessionState.READY
        self.refresh_context()  # (a model chosen during the turn now gives its window)
        self._save_now()
        self._notify("state")
        self._changed()

    # ------------------------------------------------------------------ steering and stopping

    async def steer(self, text: str, attachments: Sequence[Attachment] = ()) -> SteerOutcome:
        """Say something to the turn that is running: the model reads it at its next request."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        turn = self._turn
        if self.state is not SessionState.RUNNING or turn is None:
            return SteerOutcome(False, over=True)
        if not text:
            return SteerOutcome(False, message="没有内容")
        if turn.stopping:
            return SteerOutcome(False, message="本轮正在停止")
        try:
            item = user_item(text, attachments)
        except ValueError as error:
            return SteerOutcome(False, message=f"附件无法发送：{error}")
        block = self.transcript.add_steer(turn, text, attachments, pending=True)
        self._steers.append(Steer(text, (), block, item))
        self._changed()
        return SteerOutcome(True)

    def take_unadopted(self) -> list[tuple[str, tuple[Attachment, ...]]]:
        taken, self._unadopted = self._unadopted, []
        return taken

    def _release_steers(self) -> None:
        """The turn is over and the loop did not take these: they go back to the user."""
        turn = self._turn
        for steer in self._steers:
            block = steer.ref
            if turn is not None:
                self.transcript.remove_steer(turn, block)
            self._unadopted.append((block.text, tuple(block.attachments)))
        if self._steers:
            self._steers.clear()
            self._notify("steer")

    async def interrupt(self) -> None:
        turn, task = self._turn, self._turn_task
        if self.state is not SessionState.RUNNING or turn is None:
            return
        self.transcript.mark_stopping()
        self._changed()
        self._expire_pending()
        if self._context is not None:
            self._context.abort.set()  # (a running command is killed at once)
        if task is not None and not task.done():
            task.cancel()
        done = self._turn_done
        if done is not None and self._turn is turn:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), INTERRUPT_GRACE_SECONDS)
        if self._turn is turn:
            log.warning("the turn did not end within %.0f s of the stop request", INTERRUPT_GRACE_SECONDS)
            self._release_steers()  # (the loop never took them: back to the user, not into the next turn)
            self.transcript.abort_turn("停止超时：这里先停止显示。")
            self._record_turn("interrupted")
            # The loop of this turn is still winding down but no longer owns the conversation (``_Watch`` drops what it
            # reports). Its calls get their outputs here: the next turn must not send a history with a call that has none.
            pair_in_place(self.saved, INTERRUPTED)
            self._end_turn(turn)

    # ------------------------------------------------------------------ command: /compact

    def start_compaction(self) -> str | None:
        """``/compact``: turn the conversation so far into a summary now. It runs as a turn of its own (the page shows the command,
        the working state and the result; the stop button works) that adds nothing to the conversation but the summary.
        Why it cannot start (a sentence for the user), or ``None`` when it has started."""
        if self.state is not SessionState.READY:
            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"
        if not self.saved.items:
            return "还没有对话可以压缩。"
        self.used_at = time.time()
        self.state = SessionState.RUNNING
        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn("/compact")
        self._turn = turn
        self._turn_done = asyncio.Event()
        self.transcript.set_working_state("compacting_conversation")
        self._turn_task = self._spawn(self._run_compaction(turn))
        self._notify("state")
        self._changed()
        return None

    async def _run_compaction(self, turn: Turn) -> None:
        failure: BaseException | None = None
        done = False
        try:
            view = compact.view_of(self.saved.items, self.saved.compaction)
            done = await self._compact(turn, view, compact.estimate_items(view), False, manual=True)
        except asyncio.CancelledError:
            if self._turn is turn:
                self.transcript.abort_turn("压缩已停止，对话没有改动。")
                self._end_turn(turn)
            raise
        except Exception as error:
            failure = error
        if self._turn is not turn:
            return
        if failure is not None:
            log.warning("a compaction the user asked for failed: %s", failure)
            reason = friendly_failure(failure) if isinstance(failure, ChatGPTError) else f"{type(failure).__name__}: {failure}"
            self.transcript.fail_turn(f"没有压缩，对话没有改动：{reason}")
        else:
            if not done:
                self.transcript.add_notice(
                    "没有压缩：" + ("这段对话已经是一段摘要了。" if self.saved.compaction else "对话还太短，摘要不会比原文更小。")
                )
            self.transcript.finish_turn(turn, TurnStatus.DONE)
        self._end_turn(turn)

    # ------------------------------------------------------------------ context and usage

    def _record_marker(self) -> tuple[Any, ...]:
        record = self.saved.compaction
        return (record.get("at"), record.get("upto")) if record else ()

    def _keep_measure(self, loop: AgentLoop, model: str) -> None:
        """What the model counted at the end of the turn, for the first request of the next one (review G7)."""
        measured = loop.measured
        self._measure = (model, self._record_marker(), measured[0], measured[1]) if measured is not None else None

    def _carried_measure(self, model: str) -> dict[str, Any]:
        """What the next loop may know of the size of the conversation before its first request: the figure the model gave for
        this very view (same model, same compaction record), else a floor (the conversation has not shrunk since the last
        figure, which may be another model's or the one saved before a restart), else nothing."""
        kept = self._measure
        if kept is not None and kept[0] == model and kept[1] == self._record_marker():
            return {"measured": (kept[2], kept[3])}
        used = self.context.used
        if used and not self.context.compacted:
            return {"floor": used}
        return {}

    def refresh_context(self) -> None:
        model = self.running_model
        window = self.manager.model_window(model) or (self.context.window if self.context.model in ("", model) else None)
        if window != self.context.window or model != self.context.model:
            self._publish_context(replace(self.context, window=window, model=model))

    def _publish_context(self, state: ContextState) -> None:
        if state != self.context:
            self.context = state
            self._notify("context")

    def _add_usage(self, usage: Usage, *, measures_context: bool = True) -> None:
        tokens = usage.tokens()
        self._usage_total = tokens if self._usage_total is None else self._usage_total.plus(tokens)
        model = self.running_model
        if model:
            self._usage_models[model] = self._usage_models.get(model, Tokens()).plus(tokens)
            self.saved.usage_models = {name: value.to_json() for name, value in self._usage_models.items()}
        total = self._usage_total
        self.saved.usage = {"input": total.input_tokens, "output": total.output_tokens, "cacheRead": total.cache_read_tokens}
        if not measures_context:  # (a summary request: it counts, but it says nothing about how full the conversation is)
            self._changed()
            return
        self.saved.context_used = usage.context
        model = self.running_model
        window = self.manager.model_window(model) or self.context.window
        self._publish_context(replace(self.context, used=usage.context, window=window, model=model, compacted=False))
        self._changed()

    # ------------------------------------------------------------------ keeping inside the window

    def _summary_effort(self) -> str | None:
        """The lowest reasoning level of the model: a summary needs no deep thought, and the user's own level may be Max."""
        info = self.manager.model_info(self.running_model)
        efforts = tuple(getattr(info, "efforts", ()) or ())
        return "low" if "low" in efforts else (efforts[0] if efforts else None)

    async def _compact(
        self, owner: Turn, history: list[dict[str, Any]], estimate: int, overflow: bool, fixed: int = 0, *, must_fit: bool = False, manual: bool = False
    ) -> bool:
        """Summarise the start of the conversation so that the next request fits the model's window (``harness.compact``).
        ``history`` is the view the loop holds: on success it is replaced in place and the log remembers where the summary
        stops. ``fixed``: the tokens every request holds besides the view (the instructions and the tools). ``False``:
        nothing was changed (nothing to cut, a summary that would not do its job, or the turn is no longer this one's).

        ``must_fit``: the request cannot be sent as it is (the server refused it, or it is bigger than the window). Then a
        compaction that cannot make it fit ends the turn here (``ChatGPTError``, code ``CONTEXT_FULL``, with the reason)
        rather than send what is known to be too big.

        ``manual``: the user asked for it (``start_compaction``): the whole conversation becomes the summary, and a summary
        that cannot be written raises instead of being replaced by the digest."""
        model = self.running_model
        window = self.manager.model_window(model)
        # (refused although the estimate said it fits: aim well below what was refused; no window known: below the estimate)
        target = min(window, int(estimate * 0.7)) if window and overflow else (window or int(estimate * 0.7))
        if manual:
            target = window or MANUAL_WINDOW
        record = self.saved.compaction
        # (the loop's view is view_of(log, record): it begins with a summary because there is a record, never because of what
        # a message says, which may be a summary the user pasted)
        synthetic = compact.synthetic_count(record)
        start = int(record.get("upto") or 0) if record else 0
        self.transcript.set_working_state("compacting_conversation")
        self._changed()
        try:
            result = await compact.compact(
                history,
                stream=self.manager.stream,
                model=model,
                reasoning=self._summary_effort(),
                window=target,
                fixed=fixed,
                synthetic=synthetic,
                previous=str(record.get("summary") or "") if record else "",
                kept=compact.kept_of(record) if record else "",
                turn=len(history) if manual else synthetic + self._turn_start - start,
                on_usage=lambda usage: self._add_usage(usage, measures_context=False),
                whole=manual,
                fallback=not manual,
            )
        finally:
            self.transcript.set_working_state("thinking")
            self._changed()
        if self._turn is not owner:
            return False
        entry: dict[str, Any] | None = None
        if result is not None and result.fits:
            upto = compact.log_index(result.cut, start, synthetic)
            entry = {
                "upto": upto, "summary": result.summary, "kept": result.kept, "at": time.time(), "before": result.before,
                "after": result.after, "model": model, "mechanical": result.mechanical,
            }
            if not compact.boundary_ok(self.saved.items, upto) or compact.view_of(self.saved.items, entry) != result.view:
                # (the view and the log disagree: kept, this summary would give the next turn a different conversation,
                # or one with a tool result whose call is gone)
                log.error("a compaction of the conversation %s does not match its log (upto %d); not kept", self.session_id, upto)
                entry = None
        if entry is None:
            if must_fit:
                raise self._too_big(model, window, result, estimate, fixed, overflow)
            return False
        self.saved.compaction = entry
        history[:] = result.view
        self.saved.context_used = 0
        self._publish_context(replace(self.context, used=None, compacted=True))
        self.transcript.add_notice(self._compaction_notice(result, model, window, overflow, manual))
        log.info("compacted the conversation %s: ~%d -> ~%d tokens%s", self.session_id, result.before, result.after, " (digest only)" if result.mechanical else "")
        self._save_soon()
        self._changed()
        return True

    def _compaction_notice(self, result: compact.Compacted, model: str, window: int | None, overflow: bool, manual: bool = False) -> str:
        info = self.manager.model_info(model)
        name = info.display_name if info is not None else model
        before, after = format_tokens(result.before), format_tokens(result.after)
        if manual:
            return f"已按你的要求压缩：对话约 {before}，现在约 {after}。整段对话变成了一段摘要，下一条消息从摘要接着聊；上面的完整记录还在。"
        if overflow:
            text = f"服务器说对话太长，「{name}」读不下（约 {before}）。已压缩到约 {after}"
        elif window:
            text = f"对话约 {before}，快超出「{name}」的窗口（{format_tokens(window)}）。已压缩到约 {after}"
        else:
            text = f"对话约 {before}，已压缩到约 {after}"
        if len(result.view) == 1:
            text += "：到这里的对话（刚完成的这一步也在内）都变成了一段摘要"
            text += "，你这一轮说的话原样附在摘要后面；上面的完整记录还在。" if result.kept else "；上面的完整记录还在。"
        else:
            text += "：更早的对话变成了一段摘要，最近的原样保留；上面的完整记录还在。"
        if result.mechanical:
            text += f"摘要没写成（{result.problem}），摘要里只有你说过的话的清单。"
        return text

    def _too_big(
        self, model: str, window: int | None, result: compact.Compacted | None, estimate: int, fixed: int, overflow: bool
    ) -> ChatGPTError:
        """Why the turn ends: even compacted, its next request would not fit (the log keeps everything)."""
        info = self.manager.model_info(model)
        name = info.display_name if info is not None else model
        need = result.after + fixed if result is not None else estimate
        where = f"「{name}」的窗口（{format_tokens(window)}）" if window else f"「{name}」能读的长度"
        if result is None:
            text = f"下一次请求约 {format_tokens(need)}，超出了{where}，也没有可以压缩成摘要的更早部分"
        else:
            text = f"即使把更早的部分压缩成摘要，下一次请求仍约 {format_tokens(need)}，超出了{where}"
        if overflow:
            text = "服务器说对话太长。" + text
        text += "，所以没有发出。上面的完整记录还在；可以把消息缩短或分几次发、换一个窗口更大的模型，或开一个新会话。"
        log.warning("the conversation %s cannot be brought inside the window of %s (~%d tokens, window %s)", self.session_id, model, need, window)
        return ChatGPTError(text, code=CONTEXT_FULL)

    # ------------------------------------------------------------------ saving

    def _snapshot(self) -> SavedSession:
        saved = self.saved
        saved.project = str(self.project)
        saved.title = self.title if self.title != "新会话" else saved.title
        saved.model, saved.reasoning, saved.autonomy = self.model, self.reasoning, self.autonomy
        saved.mode = PLAN if self.mode == PLAN else ""
        saved.modified_at = self.active_at
        saved.recalled = sorted(self.recalled)
        # (a call still waiting or running has no output yet; the file must be a history the next request could send
        # even if the real output never gets written. The turns and the compaction follow their items.)
        return paired(saved)

    def _save_soon(self) -> None:
        if self._save_handle is not None or not self.saved.turns:
            return
        with contextlib.suppress(RuntimeError):
            self._save_handle = asyncio.get_running_loop().call_later(SAVE_DELAY_SECONDS, self._save_now)

    def _save_pending(self) -> None:
        if self._save_handle is not None:
            self._save_now()

    def _save_now(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if not self.saved.turns:
            return
        try:
            self.manager.sessions.save(self._snapshot())
        except OSError:
            log.warning("could not save the conversation", exc_info=True)

    # ------------------------------------------------------------------ approvals

    def _live(self) -> Turn | None:
        turn = self._turn
        return turn if turn is not None and turn.status is TurnStatus.RUNNING else None

    async def _ask(self, call: Call) -> str:
        turn = self._live()
        tools = self._tools
        if turn is None or tools is None:
            return "cancel"
        prompt = self.transcript.add_permission_prompt([call.call_id], [tools.action(call)], APPROVAL_OPTIONS)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return "cancel"
        value = await self._wait(prompt.id)
        return value or "cancel"

    async def _ask_questions(self, call: Call, views: tuple[QuestionView, ...]) -> list[str | list[str]] | None:
        """The model's ``ask_user``: the question card of the page, answered by the user (``None``: skipped, or the turn went on
        without them)."""
        if self._live() is None:
            return None
        who = "API 模型" if self.running_model.startswith("api:") else "ChatGPT Plan"
        prompt = self.transcript.add_question_prompt(call.call_id, views, who=who)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return None
        return await self._wait(prompt.id)

    async def _wait(self, prompt_id: str) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[prompt_id] = future
        try:
            return await future
        except asyncio.CancelledError:
            self._expire_prompt(prompt_id)
            raise
        finally:
            self._pending.pop(prompt_id, None)
            self._changed()

    def answer_permission(self, prompt_id: str, value: str) -> bool:
        future = self._pending.get(prompt_id)
        if future is None or not self.transcript.answer_permission(prompt_id, value):
            return False
        if not future.done():
            future.set_result(value)
        return True

    def answer_question(self, prompt_id: str, answers: Sequence[str | Sequence[str]] | None) -> bool:
        """Answer (or, with ``None``, skip) the question the model asked with ``ask_user``; each answer is a string or a list of
        strings."""
        future = self._pending.get(prompt_id)
        if future is None:
            return False
        texts = None if answers is None else [asking.answer_text(answer) for answer in answers]
        if not self.transcript.answer_question(prompt_id, texts):
            return False
        if not future.done():
            future.set_result(None if answers is None else [answer if isinstance(answer, str) else list(answer) for answer in answers])
        return True

    def _expire_prompt(self, prompt_id: str) -> None:
        self.transcript.expire_prompt(prompt_id)
        future = self._pending.pop(prompt_id, None)
        if future is not None and not future.done():
            future.set_result(None)

    def _expire_pending(self) -> None:
        for prompt_id in list(self._pending):
            self._expire_prompt(prompt_id)
        self.transcript.expire_prompts()

    def _settle_expired(self) -> None:
        for prompt_id, future in list(self._pending.items()):
            prompt = self.transcript.prompt(prompt_id)
            if prompt is None or prompt.status is not PromptStatus.PENDING:
                self._pending.pop(prompt_id, None)
                if not future.done():
                    future.set_result(None)


class _Watch(Observer):
    """What the loop reports, as the transcript the page shows.

    It belongs to one turn (``owner``). A stop that ran out of patience ends the turn while its loop is still winding
    down; that loop goes on reporting, and none of it may reach the turn that follows (its text, its tool results, its
    token counts, the steers waiting for it) or the log: once ``owner`` is not the controller's turn, every report is dropped."""

    def __init__(self, controller: ChatGPTController, tools: HarnessTools, owner: Turn, *, instructions: str = "") -> None:
        self.ctl = controller
        self.tools = tools
        self.owner = owner
        self.instructions = instructions
        """The turn's instructions: with the tool declarations, what every request holds besides the conversation."""
        self._compacted_to = 0
        """The estimate right after the last compaction of this turn (or at the last one that could not help)."""

    @property
    def current(self) -> bool:
        return self.ctl._turn is self.owner

    @property
    def _turn(self) -> Turn | None:
        return self.ctl._live() if self.current else None

    def working(self, state: str | None) -> None:
        if not self.current:
            return
        self.ctl.transcript.set_working_state({"running_tool": "executing_tool"}.get(state or "", state))
        self.ctl._changed()

    def text_delta(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self.ctl.transcript.append_text(self._turn, item_id, 0, text)
            self.ctl._changed()

    def text_done(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self.ctl.transcript.end_text(self._turn, item_id, 0, text)
            self.ctl._changed()

    def thinking_delta(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self.ctl.transcript.append_thinking(self._turn, item_id, 0, text)
            self.ctl._changed()

    def thinking_done(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self.ctl.transcript.end_thinking(self._turn, item_id, 0, text=text or None)
            self.ctl._changed()

    def call_begun(self, item_id: str, call_id: str, name: str) -> None:
        if self._turn is not None:
            ui_name, _ = self.tools.display(name, {})
            self.ctl.transcript.ensure_tool(self._turn, call_id, ui_name)
            self.ctl._changed()

    def call_ready(self, call: Call) -> None:
        turn = self._turn
        if turn is None:
            return
        ui_name, data = self.tools.display(call.name, call.args)
        tool = self.ctl.transcript.ensure_tool(turn, call.call_id, ui_name)
        self.ctl.transcript.set_tool_input(turn, tool, data, final=True)
        self.ctl._changed()

    async def ask(self, call: Call, verdict: Verdict) -> str:
        return await self.ctl._ask(call) if self.current else "cancel"

    def call_running(self, call: Call) -> None:
        if not self.current:
            return
        spec = self.tools.registry.get(call.name)
        if spec is None or spec.kind != "read":
            self.ctl._save_pending()  # (the call is on disk before it can change anything: a crash then reads "result unknown")
        turn = self._turn
        tool = turn.tools.get(call.call_id) if turn is not None else None
        if turn is not None and tool is not None:
            self.ctl.transcript.tool_running(turn, tool)
            self.ctl._changed()

    def call_done(self, call: Call, outcome: Outcome) -> None:
        if not self.current:
            return
        turn = self._turn
        status = "cancelled" if outcome.cancelled else "error" if outcome.is_error else "done"
        self.ctl.saved.tools[call.call_id] = {"status": status, "diffs": [diff_to_json(diff) for diff in outcome.diffs]}
        if turn is None:
            return
        tool = turn.tools.get(call.call_id)
        if tool is None:
            ui_name, data = self.tools.display(call.name, call.args)
            tool = self.ctl.transcript.ensure_tool(turn, call.call_id, ui_name)
            self.ctl.transcript.set_tool_input(turn, tool, data, final=True)
        if outcome.diffs:
            self.ctl.transcript.set_tool_diffs(turn, tool, outcome.diffs, source="result")
        self.ctl.transcript.finish_tool(
            turn, tool, outcome.output, status={"cancelled": ToolStatus.CANCELLED, "error": ToolStatus.ERROR}.get(status, ToolStatus.DONE)
        )
        self.ctl._changed()

    def notice(self, text: str) -> None:
        if self.current:
            self.ctl.transcript.add_notice(text)
            self.ctl._changed()

    def usage(self, usage: Usage) -> None:
        if self.current:
            self.ctl._add_usage(usage)

    def history_grew(self, items: Sequence[dict[str, Any]]) -> None:
        if self.current:
            self.ctl.saved.items.extend(items)  # (the loop works on the view; the log is what the page and the file are made of)
            self.ctl._save_soon()

    def fixed(self) -> int:
        """Tokens of what every request of the turn holds besides the conversation."""
        return compact.fixed_tokens(self.instructions, self.tools.declarations(), self.ctl.manager.shape)

    def _compact_at(self, window: int) -> int:
        """Where the user wants the conversation compacted: their percent of the window (read at every request)."""
        settings = self.ctl.manager.settings
        return int(window * settings.chatgpt_compact_at / 100)

    def tool_room(self, estimate: int, step: int) -> int | None:
        if not self.current:
            return None
        window = self.ctl.manager.model_window(self.ctl.running_model)
        if window is None:
            return None
        if not self.ctl.manager.settings.chatgpt_compact:
            # (no summary comes first: the request that carries them is sent as it is, and has to fit the window)
            return max(0, compact.budget(window) - estimate)
        # (below the point where the conversation is compacted, so that the model reads them before any summary; when that
        # point is near or past, what a compaction keeps word for word, with the answer that asked for them)
        return max(0, self._compact_at(window) - 1 - estimate, compact.keep_tokens(window) - step)

    async def prepare(self, history: list[dict[str, Any]], estimate: int, overflow: bool) -> bool:
        if not self.current:
            return False
        window = self.ctl.manager.model_window(self.ctl.running_model)
        over = False
        if not overflow:
            settings = self.ctl.manager.settings
            if not settings.chatgpt_compact:  # (the user's choice; the server's refusal still gets its compaction)
                return False
            # (no window known: nothing to compare with, the server's refusal is the signal; a second compaction in a
            # turn waits until the conversation has grown since the first, so that one that cannot help is not repeated;
            # but a request bigger than the window is not sent as it is: it is compacted, or the turn ends)
            over = window is not None and estimate > compact.budget(window)
            at = self._compact_at(window) if window is not None else 0
            if not over and (window is None or estimate < max(at, int(self._compacted_to * compact.PROGRESS))):
                return False
        fixed = self.fixed()
        done = await self.ctl._compact(self.owner, history, estimate, overflow, fixed, must_fit=overflow or over)
        # (after one that could not help too: it is not tried again before the conversation has grown)
        self._compacted_to = compact.estimate_items(history) + fixed if done else estimate
        return done

    def has_steer(self) -> bool:
        return self.current and bool(self.ctl._steers)

    def take_steer(self) -> list[Steer]:
        if not self.current:
            return []
        taken, self.ctl._steers = self.ctl._steers, []
        return taken

    def steer_given(self, steer: Steer) -> None:
        turn = self._turn
        if turn is not None:
            self.ctl.transcript.settle_steer(turn, steer.ref)
            self.ctl._changed()


__all__ = ["DEFAULT_LEVEL", "ChatGPTController", "normalize_level", "user_item"]
