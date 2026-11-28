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
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from desktop.core.attachments import DEFAULT_PROMPT, IMAGE, PDF, TEXT, Attachment, sniff_image
from desktop.core.chatgpt import history as saved_history
from desktop.core.chatgpt.archive_binding import ArchiveBinding, PendingSnapshot
from desktop.core.chatgpt.errors import AuthError, ChatGPTError, RequestNotSent, friendly_failure, is_transient
from desktop.core.chatgpt.responses import Event, Finished, Started, Usage, build_request
from desktop.core.engine_errors import EngineRpcError
from desktop.core.engine_items import attachment_block
from desktop.core.connections import ConnectionError as ExtensionError
from desktop.core.context import ContextState, format_tokens
from desktop.core.context_envelope import wrap as wrap_context
from desktop.core.fusion import recovery as fusion_recovery, verification_case
from desktop.core.fusion.backends import GUARDED
from desktop.core.fusion.candidate import MERGE_CLAUSE, Staging, mark_candidates, unflag
from desktop.core.handoff import Handoff
from desktop.core.harness import asking, compact, recall_tool, skills
from desktop.core.harness.history import prune_tool_images
from desktop.core.harness.loop import INTERRUPTED, AgentLoop, Call, Observer, Outcome, Steer, TurnEnd, Verdict
from desktop.core.harness.mcp import McpClient, McpError, McpTool, parse_tools
from desktop.core.harness.permissions import LEVELS
from desktop.core.harness.diagnostics import MAX_RECORDS, RequestTrace
from desktop.core.harness.prompt import build_instructions, changed_context, context_refresh, context_update
from desktop.core.harness.store import FIRST_PROMPT, SavedSession, SavedTurn, diff_to_json, pair_in_place, paired, recover
from desktop.core.harness.shell import ShellStatus
from desktop.core.harness.tools import ToolContext, ToolError
from desktop.core.harness.toolset import HarnessTools
from desktop.core.hooks import PromptContext, PromptEvent
from desktop.core.memory.launch import new_run
from desktop.core.prompts import APPROVAL_OPTIONS, QuestionView
from desktop.core.session_state import SESSION_STATE_LABELS, LiveSettings, SessionState, SteerOutcome
from desktop.core.toolinfo import working_label as label_for_working_state
from desktop.core.transcript import PromptStatus, QuestionPrompt, ToolStatus, Transcript, Turn, TurnStatus, UserBlock
from desktop.core.usage import Tokens

if TYPE_CHECKING:
    from desktop.core.chatgpt.engine import ChatGPTEngine
    from desktop.core.fusion.identity import Identity

log = logging.getLogger("desktop.chatgpt")

DEFAULT_LEVEL = "code"
INTERRUPT_GRACE_SECONDS = 8.0
MCP_WAIT_SECONDS = 20.0
SAVE_DELAY_SECONDS = 2.0
_CHECKPOINT_WRITERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="witty-checkpoint")
STOP_NOTES = {
    "max_output_tokens": "回答太长，被截断了；可以回复「继续」。",
    "content_filter": "回答被内容过滤停止了。",
    "empty_response": "模型连续两次没有给出文字或工具调用，本轮未完成；可以换模型或回复「继续」。",
}
CONTEXT_FULL = "context_full"
"""The code of the failure that ends a turn whose next request cannot be made to fit the model's window."""
MANUAL_WINDOW = 128_000
"""The window a compaction the user asked for is measured against when the model's is not known."""
MODES = ("auto", "spec")
PLAN = "spec"
ARCHIVE_ORIGIN = "sessionArchive"
"""The ``saved.origin`` key holding the bound session archive's ``sid``; only its presence lets ``open_saved``
rebind — a saved session without it keeps no archive, instead of being migrated silently."""
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


@dataclass
class _ResumeBatch:
    text: str
    pending: list[fusion_recovery.ResumeTarget]
    waiting: list[fusion_recovery.ResumeTarget] = field(default_factory=list)
    active: fusion_recovery.ResumeTarget | None = None


@dataclass(slots=True)
class _Checkpoint:
    writing: Future[None]
    archive: Callable[[], None] | None
    error: OSError | None = None
    settled: bool = False


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
    _speed_output: int = 0
    """Output since opening from model requests on the turn's path, excluding concurrent title requests."""
    _usage_models: dict[str, Tokens] = field(default_factory=dict)
    _trace: RequestTrace = field(default_factory=RequestTrace)
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
    _proposed_plan: str = ""
    """A plan submitted in the current turn; durable only after its call is paired and the turn ends."""
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
    _mcp_restarted: set[str] = field(default_factory=set)
    _mcp_task: asyncio.Task[None] | None = None
    _tools: HarnessTools | None = None
    _context: ToolContext | None = None
    _pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    _listeners: list[Callable[[ChatGPTController, str], None]] = field(default_factory=list)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _save_handle: asyncio.TimerHandle | None = None
    _checkpoint: _Checkpoint | None = None
    _close_requested: bool = False
    _close_task: asyncio.Task[None] | None = None
    _session_lock: Any = None
    stop_hook_blocked: bool = False
    last_turn_end: TurnEnd | None = None
    check_retry_reason: str = ""
    _recovery_delay: float | None = None
    _recovery_timer: asyncio.TimerHandle | None = None  # Current expert turn only; no parsing of localized error text.
    _fusion_resume_question: tuple[str, str, tuple[fusion_recovery.ResumeTarget, ...]] | None = None
    _fusion_resume_focus: fusion_recovery.ResumeTarget | None = None
    _fusion_resume_batch: _ResumeBatch | None = None
    _save_failed: bool = False
    _checkpoint_written: bool = False
    _first_prompt_retained: bool = False
    _fusion: Any = None
    _pending_fusion_title: tuple[str, Any, Callable[[], bool]] | None = None
    _fusion_title_task: asyncio.Task[None] | None = None
    _paused_fusion: Any = None
    _fusion_parent: Any = None
    _fusion_identity: Identity | None = None
    _fusion_read_only: bool = False
    _fusion_step_limit: int | None = None
    _acp: Any = None
    _unsent_prompt: str = ""
    _archive_binding: ArchiveBinding | None = None
    """The session archive this log is mirrored to (``chatgpt/archive_binding``): ``None`` until a new session's
    first durable checkpoint, and forever for a saved session that never had one — no implicit migration."""
    _archive_pending: bool = False
    """``open_new`` marked a real (non-expert) conversation for archiving; its first checkpoint creates the
    binding and records the ``sid`` under ``ARCHIVE_ORIGIN`` in the same write."""
    _archive_broken: bool = False
    """An archive write failed: the conversation goes on, but ``_compact`` refuses to run unarchived."""

    def __post_init__(self) -> None:
        self.saved = SavedSession(id=self.session_id, project=str(self.project))
        self._outputs = self.manager.sessions.outputs(self.session_id)

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
        if self._fusion_parent is not None:
            self._fusion_parent._notify("changed")
        else:
            self.manager._notify(self, kind)

    def _changed(self) -> None:
        prompts = self._fusion.recovery_questions() if self._fusion is not None else []
        if self._fusion_resume_question is not None:
            ident, _, targets = self._fusion_resume_question
            question = '请选择要继续的原任务。选择只确认任务，不增加授权。' if targets else '找不到可可靠接回的原任务。请取消并说明要继续哪项工作。'
            if len(targets) > 1:
                question += ' 全部续作会逐项推进，等待授权的任务仍需确认。'
            options = self._resume_options(targets)
            prompts.append(QuestionPrompt(ident, ident, (QuestionView(0, '继续原任务', question, options, False),),
                                          who='Fusion', options_only=True))
        self.transcript.set_recovery_prompts(prompts)
        self._notify("changed")

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _check_fusion_switch(self) -> None:
        from desktop.core.fusion.registry import FusionError
        if self._fusion_parent is not None or self.state is not SessionState.READY or self._turn is not None:
            raise FusionError("The conversation must be idle before switching Fusion mode.")
        if self.saved.plan.get("status") == "pending":
            raise FusionError("Decide or revise the pending plan before switching Fusion mode.")

    def enable_fusion(self, policy: Any, *, reasoning: str | None = None, retained: Any = None) -> None:
        from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
        from desktop.core.fusion.runtime import FusionRuntime, native_model
        self._check_fusion_switch()
        if not isinstance(policy, Policy) or self._fusion is not None or self.manager.model_info(native_model(policy.main)) is None:
            raise FusionError("The Fusion main model must be available; no fallback was selected.")
        runtime = self._paused_fusion
        if runtime is None:
            if retained is not None:
                if (not isinstance(retained, dict) or not isinstance(retained.get("owner"), str)
                        or type(retained.get("mainTokens", 0)) is not int):
                    raise FusionError("The retained Fusion owner is invalid.")
                registry = FusionRegistry.from_json(self.project, retained.get("registry"))
                runtime = FusionRuntime(self, registry, owner=retained["owner"], main_tokens=retained.get("mainTokens", 0))
            else:
                runtime = FusionRuntime(self, FusionRegistry(self.project, policy))
        if runtime.registry.policy != policy:
            raise FusionError("The retained Fusion configuration does not match this conversation.")
        configured = runtime.reasoning_for(policy.main)
        baseline = retained.get("settings", runtime.settings_snapshot(policy)) if retained is not None else None
        if baseline is not None and not isinstance(baseline, dict):
            raise FusionError("The retained Fusion settings snapshot is invalid.")
        self._set_fusion_mode(runtime, None, native_model(policy.main), configured if configured is not None else reasoning,
                              settings_snapshot=baseline)

    def disable_fusion(self, model: str, reasoning: str) -> None:
        from desktop.core.fusion.registry import FusionError
        self._check_fusion_switch()
        if self._fusion is None or model.startswith(GUARDED) or self.manager.model_info(model) is None:
            raise FusionError("Choose an available single-agent model before leaving Fusion.")
        self._set_fusion_mode(None, self._fusion, model, reasoning)

    async def retire_acp_context(self) -> None:
        """A successful handoff keeps the old native reference as evidence, not as the destination's context."""
        checkpoint = self.saved.origin.get("fusionAcp")
        if checkpoint is None:
            return
        archives = self.saved.origin.get("fusionAcpHistory", [])
        if not isinstance(checkpoint, dict) or not isinstance(archives, list):
            raise ChatGPTError("ACP 上下文检查点损坏，未切换模型。", code="fusion_acp_context")
        if self._acp is not None:
            await self._acp.close()
        before = self.saved.origin
        self.saved.origin = {key: value for key, value in before.items() if key != "fusionAcp"}
        self.saved.origin["fusionAcpHistory"] = [*archives, {**checkpoint, "model": self.model}]
        try:
            self._save_now(required=True)
        except BaseException:
            self.saved.origin = before
            raise

    async def release_inactive_acp(self) -> None:
        """Keep durable contexts, not unused Devin processes, when switching back to the own loop."""
        if self._acp is not None and not self.model.startswith("devin:"):
            await self._acp.close()
        if self._paused_fusion is not None:
            await self._paused_fusion.close()
        if self._fusion is not None:
            retired = [view.controller._acp for view in self._fusion.agents.values()
                       if view.record.status == "retired" and view.controller is not None
                       and view.controller._acp is not None and view.controller._acp.client is not None]
            if retired:
                await asyncio.gather(*(acp.close() for acp in retired), return_exceptions=True)

    def _set_fusion_mode(self, active: Any, paused: Any, model: str, reasoning: str | None, *, settings_snapshot: Any = None) -> None:
        before = (self._fusion, self._paused_fusion, self.requested_model, self.transcript.model, self.reasoning,
                  self.saved.model, self.saved.reasoning, self.saved.fusion, self.saved.origin, self.saved.prompt)
        runtime = active or paused
        self._fusion, self._paused_fusion = active, paused
        self.requested_model = self.transcript.model = model
        if reasoning is not None:
            self.reasoning = reasoning
        self.saved.fusion = runtime.registry.to_json()
        if active is None:
            self.saved.fusion["enabled"] = False
        self.saved.origin = {**self.saved.origin, "fusionOwner": runtime.owner, "fusionMainTokens": runtime.main_tokens}
        if settings_snapshot is not None:
            self.saved.origin["fusionSettings"] = settings_snapshot
        elif "fusionSettings" not in self.saved.origin:
            self.saved.origin["fusionSettings"] = runtime.settings_snapshot(runtime.registry.policy)
            if not before[7]:  # A newly enabled session may have an explicit per-session policy.
                settings = self.manager.settings
                self.saved.origin["fusionSettings"] = {
                    "main": settings.fusion_main, "workers": list(settings.fusion_workers), "light": settings.fusion_light,
                    "reasoning": dict(settings.fusion_reasoning), "preference": settings.fusion_preference,
                    "maxSeconds": settings.fusion_max_seconds, "maxSecondsAdaptive": settings.fusion_max_seconds_adaptive,
                }
        self.saved.prompt = {}  # Mode instructions must also change when the underlying main model stays the same.
        try:
            self._save_now(required=True)
        except BaseException:
            (self._fusion, self._paused_fusion, self.requested_model, self.transcript.model, self.reasoning,
             self.saved.model, self.saved.reasoning, self.saved.fusion, self.saved.origin, self.saved.prompt) = before
            raise
        if active is None:
            self._fusion_resume_batch = None
        if self._tools is not None:
            self._tools.fusion_runner = active.delegate if active is not None else None
        self.refresh_context()
        self._notify("settings")
        self._changed()

    @property
    def agents(self) -> dict[str, Any]:
        runtime = self._fusion or self._paused_fusion
        return runtime.agents if runtime is not None else {}

    def agent_prompts(self) -> list[Any]:
        return [prompt for view in self.agents.values() if view.controller is not None
                for prompt in view.controller.transcript.pending_prompts()]

    def answer_agent_permission(self, agent_id: str, prompt_id: str, value: str) -> bool:
        view = self.agents.get(agent_id)
        return bool(view and view.controller and view.controller.answer_permission(prompt_id, value))

    def answer_agent_question(self, agent_id: str, prompt_id: str, answers: Any) -> bool:
        view = self.agents.get(agent_id)
        return bool(view and view.controller and view.controller.answer_question(prompt_id, answers))

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
        series = {"self": self._usage_total} if self._usage_total is not None else {}
        runtime = self._fusion or self._paused_fusion
        if runtime is not None:
            series.update(runtime.usage_series())
        return series

    def usage_model_series(self) -> dict[str, tuple[str, Tokens]]:
        series = {f"model:{model}": (model, tokens) for model, tokens in self._usage_models.items()}
        runtime = self._fusion or self._paused_fusion
        if runtime is not None:
            series.update(runtime.usage_model_series())
        return series

    def _output_so_far(self) -> int:
        return self._speed_output

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

    @property
    def history_ready(self) -> bool:
        """A saved conversation, not just an allocated ID or a tentative first request."""
        return self._checkpoint_written and FIRST_PROMPT not in self.saved.origin

    async def open_new(self) -> None:
        self._claim_session()
        memory = self.manager.memory
        self.preface = memory.brief_for("chatgpt", self.project) if memory is not None else ""
        # An engine with a session root archives; one without (a bare fake manager) is never even asked to.
        self._archive_pending = self._fusion_parent is None and isinstance(getattr(self.manager, "root", None), Path)
        self._ready()

    async def open_saved(self) -> None:
        self._claim_session()
        self._notify("state")  # announce the opening tab so its cached history can be shown before reading the archive
        saved = await asyncio.to_thread(self.manager.sessions.load, self.session_id)
        if saved is None:
            raise ChatGPTError("这个会话的存档找不到了。", code="missing_session")
        recovered = await asyncio.to_thread(recover, saved)
        if saved.origin.pop(FIRST_PROMPT, None) is True:
            # The process died before it could tell whether the first dispatch reached the model. Preserve it.
            recovered = True
        recovered_plan = saved.plan.get("status") == "approved" and saved.plan.get("execution") == "starting"
        if recovered_plan:
            saved.plan = {"id": uuid.uuid4().hex, "text": saved.plan["text"], "status": "pending",
                          "warning": "上次批准后会话在启动阶段中断，可能已有副作用；请先检查，再明确选择是否执行。"}
            saved.mode = PLAN
        self.saved = saved
        archive_sid = saved.origin.get(ARCHIVE_ORIGIN)
        if isinstance(archive_sid, str):
            try:
                self._archive_adopt(ArchiveBinding.restore(self.manager, archive_sid, self.session_id))
            except Exception:
                # A damaged or unreadable archive must not take the conversation down with it — but a session
                # that HAD one must not read as legacy either: broken, so compaction stays refused on the
                # whole uncompacted view instead of running unarchived.
                self._archive_fail()
        self._checkpoint_written = True
        self.requested_model = saved.model
        self.reasoning = saved.reasoning
        self.autonomy = normalize_level(saved.autonomy) or DEFAULT_LEVEL
        self.mode = PLAN if saved.mode == PLAN else "auto"
        self.saved_title = saved.title or self.saved_title
        self.transcript.model = saved.model or None
        builder, outputs, registry = self.transcript.history_builder(), self._outputs, self.manager.registry

        def replay() -> list[Turn]:
            shown_items = outputs.expand(saved.items, strict=False)
            return saved_history.build_turns(builder, registry, replace(saved, items=shown_items))

        self.transcript.adopt_history(builder, await asyncio.to_thread(replay))
        usage = saved.usage
        if usage:
            self._usage_total = Tokens.of(usage)
        self._usage_models = {model: tokens for model, raw in saved.usage_models.items() if (tokens := Tokens.of(raw)) is not None}
        if self._usage_total is not None and "cacheWrite" not in usage and self._usage_models:
            attributed = Tokens()
            for tokens in self._usage_models.values():
                attributed = attributed.plus(tokens)
            total = self._usage_total
            if (attributed.input_tokens, attributed.output_tokens, attributed.cache_read_tokens) == (total.input_tokens, total.output_tokens, total.cache_read_tokens):
                # Older archives omitted Cache Write from the total, but their complete per-model counters may know it.
                self._usage_total = replace(total, cache_creation_tokens=attributed.cache_creation_tokens)
                saved.usage = self._usage_total.to_json()
        self.context = ContextState(used=saved.context_used or None, window=self.manager.model_window(self.model), model=self.model)
        self.saved_settings = LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode=self.mode)
        if saved.fusion:
            from desktop.core.fusion.registry import FusionError, FusionRegistry
            from desktop.core.fusion.runtime import FusionRuntime, native_model
            from desktop.core.fusion.title import ATTEMPT
            try:
                registry = FusionRegistry.from_json(self.project, saved.fusion)
                if saved.origin.get(ATTEMPT) == "pending":
                    saved.origin[ATTEMPT] = "attempted"
                    registry.unmetered = registry.had_unknown = True
                enabled = saved.fusion.get("enabled", True)
                if (not isinstance(enabled, bool) or self._fusion_parent is not None
                        or enabled and native_model(registry.policy.main) != self.model):
                    raise FusionError("The Fusion mode or fixed main model does not match the saved session.")
                runtime = FusionRuntime(self, registry)
                self._fusion, self._paused_fusion = (runtime, None) if enabled else (None, runtime)
                if enabled:
                    self._fusion_resume_focus = fusion_recovery.restored_focus(saved, registry.plans)
                    await runtime.reconcile_checkpoints()
                runtime._checkpoint()
            except FusionError as error:
                raise ChatGPTError("Fusion 状态损坏或主模型不一致，已暂停恢复，原始存档未被覆盖。", code="fusion") from error
        if recovered_plan:
            await self._save_durable(required=True)
            self.transcript.add_notice(saved.plan["warning"])
        if recovered:
            log.warning("the ChatGPT conversation %s was saved in the middle of a turn (the application closed or crashed); repaired", self.session_id)
            writing = asyncio.create_task(asyncio.to_thread(self.manager.sessions.save, deepcopy(saved)))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                with contextlib.suppress(OSError):
                    await asyncio.shield(writing)
                raise
            except OSError:
                log.warning("could not save the repaired conversation", exc_info=True)
        if saved.goal:
            self.transcript.add_notice("未完成目标已保留；重新打开后不会自动执行。用 /goal resume 明确继续，或 /goal off 清除。")
        self._ready()

    def _ready(self) -> None:
        self._mcp_task = self._spawn(self._start_servers())
        self.state = SessionState.READY
        self.refresh_context()
        self._notify("state")
        self._changed()
        self._schedule_fusion_recovery()

    async def _start_servers(self) -> None:
        """Start independent servers concurrently; cached contracts are available before handshakes finish."""
        from desktop.core.memory.server import INSTRUCTIONS, SERVER_NAME, TOOLS
        from desktop.core.harness.diagnostics import fingerprint

        try:
            commands = await asyncio.to_thread(self.manager.memory_servers, self)
        except ExtensionError:
            # One broken Connections definition must not make the model conversation unusable.
            memory = self.manager.memory
            built_in = memory.server_for("chatgpt", self.project, self.run) if memory is not None else None
            commands = [built_in] if built_in is not None else []
            self.transcript.add_notice("第三方 MCP 配置未就绪，已暂时跳过；模型会话仍可继续。请检查 Connections 的定义和环境变量。")
        if self._close_requested:
            return
        contracts = self.saved.prompt.setdefault("mcp", {})
        launches = []
        for command in commands:
            client = McpClient(command.name, command.command, command.args, command.env, cwd=str(self.project))
            client.trusted_annotations = getattr(command, "trusted_annotations", False)
            key = fingerprint([command.name, command.command, command.args, command.env])
            cached = contracts.get(key, {})
            if client.trusted_annotations and command.name == SERVER_NAME:
                client.instructions = INSTRUCTIONS
                client.tools = [McpTool(SERVER_NAME, tool["name"], tool["description"], tool["inputSchema"], tool["annotations"]) for tool in TOOLS]
            elif cached:
                try:
                    if not isinstance(cached, dict) or not isinstance(cached.get("instructions", ""), str):
                        raise McpError("invalid cached contract")
                    client.tools = parse_tools(client.name, cached.get("tools", []), schema_key="schema")
                    client.instructions = cached.get("instructions", "")
                except McpError:
                    self.transcript.add_notice("已忽略损坏的 MCP 声明缓存，正在重新读取；会话仍可继续。")
            self._clients.append(client)
            launches.append((client, key))

        async def start(client: McpClient, key: str) -> None:
            try:
                await client.start()
                contracts[key] = {"instructions": client.instructions, "tools": [
                    {"name": t.name, "description": t.description, "schema": t.input_schema, "annotations": t.annotations} for t in client.tools
                ]}
            except McpError:
                log.warning("a tool server could not be started for a conversation")
                self.transcript.add_notice("工具服务器未能启动，相关调用会返回不可用；不会静默移除已知工具。")
                self._changed()
            if self._close_requested:
                await client.close()
        await asyncio.gather(*(start(client, key) for client, key in launches))

    def turn_step_limit(self) -> int:
        maximum = self.manager.settings.max_steps
        return maximum if self._fusion_step_limit is None else min(maximum, self._fusion_step_limit)

    async def _ensure_tools(self) -> HarnessTools:
        if self._mcp_task is not None and not self._mcp_task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._mcp_task), MCP_WAIT_SECONDS)
        if self._mcp_task is None or self._mcp_task.done():
            for client in self._clients:
                if client.name == "wittycode-memory" and not client.running and client.name not in self._mcp_restarted:
                    self._mcp_restarted.add(client.name)
                    self.transcript.add_notice("共享记忆连接已断开，正在尝试重启一次；工具声明保持不变。")
                    try:
                        await client.start()
                    except McpError:
                        self.transcript.add_notice("共享记忆重启失败，本会话的记忆工具暂不可用。")
                    self._changed()
        from desktop.core.settings import default_settings_path
        providers = getattr(getattr(self.manager, "api", None), "providers", None)
        protected = [self.manager.root, default_settings_path().parent]
        protected += [self.manager.root.parent / name for name in ("models.json", "providers.json", "connections.json", "settings.json", "memory", "conversations", "cache", "fusion-drive")]
        for name in ("models_file", "sidecar"):
            path = getattr(providers, name, None)
            if isinstance(path, Path):
                protected.append(path)
        for owner, name in ((getattr(self.manager, "connections", None), "file"), (self.manager.memory, "root")):
            path = getattr(owner, name, None)
            if isinstance(path, Path):
                protected.append(path)
        self._context = self._context or ToolContext(self.project, threading.Event(), protected=tuple(protected), outputs=self._outputs)
        if self._fusion_parent is None and self._fusion_identity is None:
            # Record the recall contract before preparing the first request, not at the archive checkpoint.
            recall_tool.bind_history(self._context, self)
        info = self.manager.model_info(self.running_model)
        self._context.supports_images = info is not None and info.supports_images
        names = getattr(providers, "secret_environment_names", None)
        if names is not None:
            from desktop.core.providers import ProviderError
            try:
                self._context.secret_env = await asyncio.to_thread(names)
            except (ProviderError, OSError):
                from desktop.core.child_env import environment
                # Broken unrelated provider metadata must not poison Plan sessions or leak an unknown variable name.
                self._context.secret_env = tuple(set(environment()) - set(environment(minimal=True)))
                self.transcript.add_notice("模型配置无法读取；本轮命令仅继承基础环境。会话仍可继续，请检查 API Provider 配置。")
        if self._tools is None:
            if self._mcp_task is not None and not self._mcp_task.done() and (not self._clients or any(not c.tools for c in self._clients)):
                self.transcript.add_notice("部分 MCP 工具仍在启动；本次工具清单已固定。待服务器就绪后，重新打开会话可使用新工具。")
            self._tools = HarnessTools(self.manager.registry, self._context, self.autonomy, self._clients, asker=self._ask_questions)
            self._tools.shell_progress = self._shell_progress
            self._tools.plan_submitter = self._submit_plan
            if self._fusion_identity is not None:
                self._tools.identity_tools = self._fusion_identity.tools
            if self._fusion is not None:
                self._tools.fusion_runner = self._fusion.delegate
        # The offered manifest is fixed for this open session. A first-time server that misses startup can be used
        # after reopening (its contract is cached), rather than invalidating every previous request's cache prefix.
        self._tools.level = self.autonomy
        self._tools.strict_context = self._fusion is not None or self._fusion_parent is not None
        fusion = self._fusion or getattr(self._fusion_parent, "_fusion", None)
        reporting = fusion.report_runs.get(self.session_id) if fusion is not None and self._fusion_identity is not None else None
        self._tools.report_submitter = reporting.submit if reporting is not None else None
        reader = fusion.read_record if fusion is not None and fusion.drive_enabled else None
        rating = self._fusion if self._fusion is not None and self._fusion.rating_enabled else None
        used = reporting.used if reporting is not None else rating.main_used if rating is not None else None
        self._tools.record_reader = partial(reader, used=used) if reader is not None and used is not None else reader
        self._tools.rating_submitter = rating.rate_records if rating is not None else None
        self._tools.fusion_brief_runner = self._fusion.delegate_brief if self._fusion is not None and self._fusion.brief_enabled else None
        routing = self._fusion if self._fusion is not None and self._fusion.gate("route") else None
        self._tools.route_submitter = partial(self._submit_route, routing.submit_route) if routing is not None else None
        self._tools.unit_runner = routing.delegate_unit if routing is not None and routing.unit_tools_available else None
        self._tools.route_guard = routing.route_guard if routing is not None else None
        self._tools.route_edit = routing.note_edit if routing is not None else None
        mapping = self._fusion if self._fusion is not None and self._fusion.map_available else None
        self._tools.map_reader = mapping.read_project_map if mapping is not None else None
        mapping = mapping if mapping is not None and mapping.map_enabled else None
        self._tools.map_chooser = mapping.choose_map_location if mapping is not None else None
        self._tools.map_updater = mapping.update_map_entry if mapping is not None else None
        self._tools.read_only_brief = self._fusion_read_only
        debating = self._fusion if self._fusion is not None and self._fusion_identity is None and self._fusion.gate("v3") else None
        self._tools.topic_declarer = self._declare_topic if debating is not None else None
        self._tools.declare_state = self._declare_state if debating is not None else None
        self._tools.work_plan_submitter = self._submit_work_plan if debating is not None else None
        return self._tools

    def _shell_progress(self, call: Call, status: ShellStatus) -> None:
        turn = self._turn
        item = turn.tools.get(call.call_id) if turn is not None else None
        if turn is None or item is None or item.terminal:
            return
        output = ("暂未收到输出" if status.output_idle is None else f"距最近输出约 {int(status.output_idle)} 秒")
        self.transcript.tool_running(turn, item, f"命令仍在运行：已用 {int(status.elapsed)} 秒；{output}。")
        self._changed()

    def _declare_state(self) -> dict[str, Any] | None:
        """The running turn's declaration receipt; a pending one has no ``intent`` yet and unlocks nothing."""
        receipt = getattr(self, "_declare_receipt", None)
        return receipt if isinstance(receipt, dict) and receipt.get("turn") is self._turn else None

    def _fusion_feedback_turn(self, turn: Turn | None) -> SavedTurn | None:
        if (self._fusion is None or self._fusion_identity is not None or turn is None or turn is not self._turn
                or not turn.running or not self.saved.turns):
            return None
        record = self.saved.turns[-1]
        return record if record.start == self._turn_start and record.finished is None else None

    def _fusion_opening(self, turn: Turn | None, text: str) -> None:
        if text and (record := self._fusion_feedback_turn(turn)) is not None and not record.opening:
            assert turn is not None
            record.opening = text
            self.transcript.set_turn_opening(turn, text)
            self._save_soon()
            self._changed()

    def _fusion_expert_started(self, turn: Turn | None, label: str) -> None:
        if (record := self._fusion_feedback_turn(turn)) is not None and label not in record.dispatches:
            assert turn is not None
            record.dispatches.append(label)
            self.transcript.add_turn_dispatch(turn, label)
            self._save_soon()
            self._changed()

    async def _declare_topic(self, kind: str, intent: str, topic: str, next_step: str = "") -> Outcome:
        """The main agent's one routing declaration per turn; a rename cannot reopen it this turn."""
        receipt = self._declare_state()
        if receipt is not None:
            task = receipt.get("task")
            if task is None:
                return receipt["outcome"]
            try:
                return await task  # A parallel declare shares the spend instead of re-running it.
            except Exception as error:
                return Outcome(f"Declare failed: {type(error).__name__}.", True)
        turn = self._turn
        if (intent == 'execute' and self._fusion is not None and self._fusion.unit_tools_available
                and (binding := self._fusion.pending_approved_units())):
            outcome = Outcome('Continue the approved frozen plan; activate it with submit_route({}). No units were reselected.')
            self._declare_receipt = {'turn': turn, 'kind': 'complex', 'intent': intent, 'workplan': binding, 'outcome': outcome}
            self._fusion_opening(turn, next_step)
            return self._record_fusion_declaration(self._declare_receipt)
        task = asyncio.ensure_future(self._route_topic(kind, intent, topic))
        self._declare_receipt = {"turn": turn, "task": task}
        self._fusion_opening(turn, next_step)
        try:
            outcome = await task
        except Exception as error:
            outcome = Outcome(f"Declare failed: {type(error).__name__}.", True)
        self._declare_receipt = {"turn": turn, "kind": kind, "intent": intent,
                                 "failed": outcome.is_error, "outcome": outcome}
        return self._record_fusion_declaration(self._declare_receipt)

    def _record_fusion_declaration(self, receipt: dict) -> Outcome:
        record = self.saved.turns[-1] if self.saved.turns else None
        if (receipt['intent'] == 'execute' and not receipt['outcome'].is_error and record is not None
                and record.start == self._turn_start and record.finished is None):
            record.fusion_task = record.fusion_task or uuid.uuid4().hex
            record.fusion_kind = receipt['kind']
            try:
                self._save_now(required=True)
            except ChatGPTError:
                record.fusion_task = record.fusion_kind = ''
                receipt.update(failed=True, outcome=Outcome('Could not save the Fusion task before dispatch.', True))
        return receipt['outcome']

    async def _route_topic(self, kind: str, intent: str, topic: str) -> Outcome:
        from desktop.core.fusion import debate_bridge
        from desktop.core.fusion.debate import DebateError
        from desktop.core.fusion.registry import FusionError
        if self._fusion is None:
            return Outcome("Fusion mode is not active; answer directly.", True)
        if kind == "simple":
            text = "Declared simple: no debate. Follow the declared intent; execution can still delegate to Coder."
        else:
            try:
                result = await debate_bridge.discuss(self, topic)
            except (DebateError, FusionError) as error:
                # A failed declare stays failed: this turn cannot plan or delegate off it.
                return Outcome(f"Declare failed: {error} Do not plan or delegate on this declaration.", True)
            text = debate_bridge.result_text(result)
        if intent == "plan" or kind == "complex" and intent == "execute":
            text += "\n\n" + await self._team_snapshot()
        return Outcome(text)

    async def _team_snapshot(self) -> str:
        """Local model profiles of the Fusion seats, read-only, for planning and work split."""
        from desktop.core.fusion import profiles
        from desktop.core.fusion.registry import FusionError
        from desktop.core.settings import default_settings_path
        if self._fusion.unit_enabled:
            from desktop.core.fusion.planning import UNIT_CONTRACT
            return UNIT_CONTRACT
        policy = self._fusion.registry.policy

        def effort(model: str, role: str) -> str:
            try:
                return self._fusion.reasoning_for(model, role) or ""
            except FusionError:
                return ""

        seats = {"main": (policy.main, effort(policy.main, "main")),
                 "worker": (policy.workers[0], effort(policy.workers[0], "worker")),
                 "light": (policy.light, effort(policy.light, "light"))}
        return await asyncio.to_thread(profiles.team_snapshot, default_settings_path().parent, seats)

    async def _submit_work_plan(self, data: Mapping[str, Any]) -> Outcome:
        """Validate the turn's V3 work plan into the receipt that gates routing, delegation and writes."""
        from desktop.core.fusion import planning
        receipt = self._declare_state()
        if not isinstance(receipt, dict) or receipt.get("failed"):
            return Outcome("The turn's declaration failed, so it may not plan, route, delegate or change anything.", True)
        if receipt.get("intent") != "plan" and not (receipt.get("intent") == "execute" and receipt.get("kind") == "complex"):
            return Outcome("submit_work_plan follows a plan or complex-execute declaration.", True)
        if isinstance(receipt.get("workplan"), Mapping):
            return Outcome("This turn already has a fixed work plan; it cannot be replaced.", True)
        if self._fusion.unit_enabled or (set(data) == {'continue'} and self._fusion.unit_tools_available):
            return await self._fusion.submit_units(data, receipt)
        try:
            workplan = planning.check(data)
        except ValueError as error:
            return Outcome(f"Work plan refused: {error}", True)
        receipt["workplan"] = workplan
        return Outcome("Work plan validated and recorded for this turn; a route's items now come from it, not from "
                       "the model a second time. The JSON is internal planning data — answer the user in prose.")

    async def _submit_route(self, submit: Callable[[Mapping[str, Any]], Awaitable[Outcome]],
                            args: Mapping[str, Any]) -> Outcome:
        """A frozen route's items come from the turn's validated work plan, never re-scored by the model."""
        receipt = self._declare_state()
        workplan = receipt.get("workplan") if isinstance(receipt, dict) else None
        if isinstance(workplan, dict) and 'frozen' not in workplan:
            items = [{"id": item["id"], "workstream": item["workstream"], "role": "worker",
                      "score": item["n"], "reason": item["evidence"], "paths": list(item["paths"]),
                      "deps": list(item["deps"]), "acceptance": item["acceptance"]}
                     for item in workplan["items"]]
            args = {**args, "items": items}
        return await submit(args)

    def _claim_session(self) -> None:
        try:
            self._session_lock = self.manager.sessions.claim(self.session_id)
        except OSError as error:
            raise ChatGPTError(str(error), code="session_in_use") from error

    def _release_session(self) -> None:
        if self._session_lock is not None:
            self.manager.sessions.release(self._session_lock)
            self._session_lock = None

    def _abandon(self, error: BaseException) -> None:
        self._release_session()
        self.state = SessionState.CLOSED
        self.last_error = friendly_failure(error) if isinstance(error, ChatGPTError) else f"{type(error).__name__}: {error}"
        self.manager._forget(self)
        self._notify("state")

    # ------------------------------------------------------------------ closing

    async def close(self) -> None:
        if self._close_task is None:
            self._close_requested = True
            if self._recovery_timer is not None:
                self._recovery_timer.cancel()
                self._recovery_timer = None
            self._close_task = asyncio.ensure_future(self._do_close())
        await asyncio.shield(self._close_task)

    async def _do_close(self) -> None:
        title_task = self._cancel_fusion_title()
        task = self._turn_task
        if task is not None and not task.done():
            self._expire_pending()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), INTERRUPT_GRACE_SECONDS)
        if title_task is not None:
            # Settle usage and release the independent transport before releasing the archive lock.
            await asyncio.gather(title_task, return_exceptions=True)
        runtime = self._fusion or self._paused_fusion
        if runtime is not None:
            await runtime.close()
        if self._acp is not None:
            await self._acp.close()
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
        try:
            await self._save_durable()
        finally:
            self._release_session()
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
        if self._fusion is not None and model and str(model) != self.model:
            self.transcript.add_notice("Fusion 主模型只能通过「更换主模型」明确确认。")
            self._changed()
            return False
        if self._fusion is not None and autonomy is not None and any(view.record.status == "running" for view in self.agents.values()):
            self.transcript.add_notice("专家任务运行时不能改变权限；请先停止或等待它结束。")
            self._changed()
            return False
        if autonomy is not None:
            level = normalize_level(autonomy)
            if level is None:
                self.transcript.add_notice(f"设置更新失败：没有「{getattr(autonomy, 'value', autonomy)}」这个权限档位。")
                self._changed()
                return False
            if level != self.autonomy:
                if self._fusion is not None:
                    for view in self.agents.values():
                        if view.controller is not None and view.controller._tools is not None:
                            view.controller._tools.remembered.clear()
                if self._fusion_parent is not None and self._tools is not None:
                    self._tools.remembered.clear()
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

    def _submit_plan(self, text: str) -> None:
        if self._proposed_plan:
            raise ValueError("This turn already submitted a plan.")
        self._proposed_plan = text
        if self._fusion is not None:
            self._fusion.metric_plan_proposal()

    def approve_plan(self, plan_id: str) -> bool:
        plan = self.saved.plan
        if (self.state is not SessionState.READY or self.mode != PLAN or plan.get("status") != "pending"
                or plan.get("id") != plan_id or not isinstance(plan.get("text"), str)):
            return False
        previous = plan
        bound = self.saved.origin.get("fusionWorkPlan")
        workplan = bound.get("workplan") if isinstance(bound, Mapping) and bound.get("plan") == plan["id"] else None
        self.saved.plan = {"id": plan["id"], "text": plan["text"], "status": "approved", "execution": "starting"}
        self.mode = "auto"
        try:
            self._save_now(required=True)
        except BaseException:
            self.saved.plan = previous
            self.mode = PLAN
            raise
        old_task = self._turn_task
        try:
            if self.send_prompt("请执行我刚批准的方案。", approved_plan=plan["text"], approved_workplan=workplan):
                return True
        except BaseException:
            self._restore_unstarted_plan(previous, old_task)
            raise
        self._restore_unstarted_plan(previous, old_task)
        return False

    def _restore_unstarted_plan(self, plan: dict[str, str], old_task: asyncio.Task[Any] | None) -> None:
        # A synchronous start failure must not leave a phantom running turn or consume the only approval button.
        if self._turn_task is not old_task and self._turn_task is not None:
            self._turn_task.cancel()
        if self._turn is not None:
            self.transcript.abort_turn("启动失败，计划仍等待确认。")
        self._turn = None
        if self._turn_done is not None:
            self._turn_done.set()
        self._turn_model = self._turn_reasoning = self._turn_mode = ""
        self.state = SessionState.READY
        self.mode = PLAN
        self.saved.plan = plan
        self._save_now(required=True)
        self._notify("state")
        self._changed()

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str | None:
        if any(item.kind == IMAGE for item in attachments):
            info = self.manager.model_info(self.model)
            if info is not None and not info.supports_images:
                return f"当前模型「{info.display_name}」不支持图片输入：请先移除图片，或换一个支持图片的模型。"
        for item in attachments:
            if item.kind == PDF and not (item.parsed_text or "").strip():
                return f"ChatGPT 只能读 PDF 里的文字，「{item.name}」里没有提取到文字：请换成图片或文本文件再发送。"
        return None

    @property
    def fusion_resume_waiting(self) -> bool:
        return (self._fusion_resume_question is not None or self._fusion_resume_focus is not None
                or self._fusion_resume_batch is not None and self.state is SessionState.READY)

    @staticmethod
    def _resume_options(targets: tuple[fusion_recovery.ResumeTarget, ...]) -> tuple[str, ...]:
        options = tuple(f'{n}. {target.label}' for n, target in enumerate(targets, 1))
        return (*options, '全部续作') if len(targets) > 1 else options

    def _focus_fusion_resume(self, target: fusion_recovery.ResumeTarget) -> None:
        focus = {'task': target.task, 'plan': target.plan, 'after': self.saved.turns[-1].start}
        before = self.saved.origin.get(fusion_recovery.RESUME_FOCUS)
        self.saved.origin[fusion_recovery.RESUME_FOCUS] = focus
        try:
            self._save_now(required=True)
        except ChatGPTError:
            if before is None:
                self.saved.origin.pop(fusion_recovery.RESUME_FOCUS, None)
            else:
                self.saved.origin[fusion_recovery.RESUME_FOCUS] = before
            raise
        self._fusion_resume_focus = target
        self._fusion_resume_question = None
        self.transcript.add_notice('Fusion：已找到原任务，仍在等待原工单的授权或可用条件。')
        self._changed()

    def _current_resume_target(self, target: fusion_recovery.ResumeTarget) -> fusion_recovery.ResumeTarget | None:
        _, candidates = fusion_recovery.resume_targets(self.saved, self._fusion.registry.plans, selected_task=target.task)
        return next((t for t in candidates if (t.task, t.kind, t.plan) == (target.task, target.kind, target.plan)), None)

    def _resume_notice(self, target: fusion_recovery.ResumeTarget, details: Sequence[str] = ()) -> str:
        note = 'Continue the original task from its checkpoint. Do not replay tools with unknown effects.'
        if details:
            note += '\n' + '\n'.join(details)
        if target.plan:
            plan = self._fusion.registry.plans[target.plan]
            return note + '\nOriginal goal: ' + plan['spec']['goal'] + '\nUnit state:\n' + '\n'.join(verification_case.result_lines(plan)[0])
        return note + '\nOriginal task: ' + target.label

    def send_prompt(self, text: str, attachments: Sequence[Attachment] = (), *, approved_plan: str = "",
                    approved_workplan: Mapping[str, Any] | None = None, packed_context: bool = False,
                    packed_hooks: PromptContext | None = None, recovery: bool = False,
                    resume_target: fusion_recovery.ResumeTarget | None = None) -> bool:
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        if not text or self.state is not SessionState.READY:
            return False
        if self.saved.plan.get("status") == "pending":
            if self.mode != PLAN:
                self.transcript.add_notice("计划仍待确认；请切回 Plan 并明确执行或修改，切换模式和普通回复不会批准计划。")
                self._changed()
                return False
            previous = self.saved.plan
            self.saved.plan = {}
            try:
                self._save_now(required=True)
            except BaseException:
                self.saved.plan = previous
                raise
        rt = self._fusion
        if self._fusion_resume_batch is not None and not recovery and resume_target is None:
            if not attachments and fusion_recovery.resume_text(text):
                return True  # the same explicit request is already pending
            self._fusion_resume_batch = None  # a new user turn supersedes the pending selection
        if (rt is not None and self._fusion_parent is None and self.mode != PLAN and not approved_plan and not recovery
                and (rt.registry.plans or rt.gate('v3') and rt.gate('inject'))
                and not attachments and not (self.handoff and not self.handoff.empty) and self.saved.turns
                and (resume_target is not None or fusion_recovery.resume_text(text))):
            if resume_target is not None:
                resume_target = self._current_resume_target(resume_target)
                if resume_target is None:
                    return False
                candidates = ()
            else:
                resume_target, candidates = fusion_recovery.resume_targets(self.saved, rt.registry.plans)
            if resume_target is None:
                if self._fusion_resume_question is None:
                    self._fusion_resume_question = ('fusion-resume-' + uuid.uuid4().hex, text, candidates)
                self._changed()
                return True
            if resume_target.plan:
                plan = rt.registry.plans[resume_target.plan]
                if not rt.unit_tools_available:
                    self.transcript.add_notice('原冻结工单当前不可用；没有改走旧式派工。')
                    if self._fusion_resume_question is None:
                        self._fusion_resume_question = ('fusion-resume-' + uuid.uuid4().hex, text, ())
                    self._changed()
                    return True
                if not fusion_recovery.can_start_manual(plan):
                    self._focus_fusion_resume(resume_target)
                    self._schedule_fusion_recovery()
                    return True
        self._fusion_resume_question = None
        self._fusion_resume_focus = None
        # A Fusion pack already carries brief-based recall instead of the memory preface.
        preface = "" if packed_context else self.preface
        context = packed_hooks or self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, frozenset(self.recalled), first=bool(preface),
                                                        internal=self._fusion_parent is not None))
        sent, carried, outgoing = text, None, attachments
        handoff = self.handoff if self.handoff is not None and not self.handoff.empty else None
        if handoff is not None:
            sent = handoff.prefix + text
            carried = (handoff.carried, handoff.truncated) if handoff.carried else None
            outgoing = tuple(handoff.attachments) + attachments
        approval = "The user explicitly approved this plan for execution:\n\n" + approved_plan if approved_plan else ""
        resume_context = self._resume_notice(resume_target) if resume_target is not None and not recovery else ""
        sent += wrap_context(preface, context.text, approval, resume_context)  # (after the message: see core/context_envelope)
        self._proposed_plan = ""
        self._pending_fusion_title = None
        self._first_prompt_retained = False
        self.used_at = self.active_at = time.time()
        self.state = SessionState.RUNNING
        self.stop_hook_blocked = False
        self.last_turn_end = None
        self.check_retry_reason = ""
        self._recovery_delay = None
        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode
        self._turn_start = len(self.saved.items)  # setup failures never belong to the previous turn
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn(saved_history.FUSION_RESUMING if recovery else text, attachments, handoff=carried,
                                          system_notice=recovery)
        if context.notice:
            self.transcript.add_notice(context.notice)
        self._handoff_turn = turn if handoff is not None else None
        self._preface_turn = turn if self.preface else None
        self._recall_turn, self._recall_ids = turn, context.ids
        self._turn = turn
        if recovery or resume_target is not None and resume_target.plan:
            turn.fusion_outcome = 'continuing'
        if resume_target is not None:
            workplan = {'frozen': resume_target.plan, 'budget': rt.registry.plans[resume_target.plan]['spec']['budget']} if resume_target.plan else None
            self._declare_receipt = {'turn': turn, 'kind': resume_target.kind, 'intent': 'execute',
                                     'task_id': resume_target.task, 'resume_source': 'automatic' if recovery else 'manual',
                                     'workplan': workplan,
                                     'outcome': Outcome(f'Continue the original {resume_target.kind} task; the declaration is already bound.')}
            if not recovery:
                self.transcript.add_notice(saved_history.FUSION_RESUMING)
        elif approved_plan:
            # The user's approval is this turn's execute declaration; the model cannot mint one itself. It carries
            # the work plan bound to the approved version — an older file or a reopened plan has none, so that
            # approval may answer in this turn but never executes.
            if isinstance(approved_workplan, Mapping):
                self._declare_receipt = {"turn": turn, "kind": "complex", "intent": "execute", "approved": True,
                                         "workplan": dict(approved_workplan),
                                         "outcome": Outcome("The approved plan is this turn's declared intent: execute it.")}
            else:
                self._declare_receipt = {"turn": turn, "kind": "complex", "intent": "discussion", "approved": True,
                                         "outcome": Outcome("No validated work plan is bound to this approval; "
                                                            "answer the user — this turn cannot execute.")}
        if self._fusion is not None:
            self._fusion.start_metrics(turn)
            self._fusion.start_ratings()
            self._fusion.start_route_turn()
        self._turn_done = asyncio.Event()
        self.transcript.set_working_state("thinking")
        self._turn_task = self._spawn(self._run_turn(turn, sent, outgoing, **({'packed_connections': True} if packed_hooks else {})))
        self._notify("state")
        self._changed()
        return True

    def _include_project_rules(self) -> bool:
        parent_fusion = getattr(self._fusion_parent, "_fusion", None)
        return not (self._fusion_identity is not None and self._fusion_identity.name in ("Runner", "Observer")
                    and parent_fusion is not None and parent_fusion.brief_enabled and parent_fusion.gate("inject"))

    @property
    def fusion_title_attempted(self) -> bool:
        from desktop.core.fusion.title import ATTEMPT
        return ATTEMPT in self.saved.origin

    @property
    def fusion_title_running(self) -> bool:
        return self._fusion_title_task is not None and not self._fusion_title_task.done()

    def prepare_fusion_title(self, question: str, allowed: Callable[[], bool]) -> None:
        from desktop.core.fusion.title import source
        if self._live() is None or self.fusion_title_running:
            return
        try:
            if (account := source(self, self.running_model)) is not None:
                self._pending_fusion_title = (question, account, allowed)
        except Exception as error:
            log.info("Could not identify the Fusion title source: %s", type(error).__name__)

    def _check_fusion_title_source(self, *, native_account: str | None = None) -> None:
        """Do not name under the selection at send time if the main transport picked up another source."""
        candidate = self._pending_fusion_title
        if candidate is None:
            return
        from desktop.core.fusion.title import source
        model = self.running_model
        try:
            if (source(self, model) == candidate[1]
                    and (not model.startswith("devin:") or native_account == candidate[1][1])):
                return
        except Exception as error:
            log.info("Could not verify the Fusion title source: %s", type(error).__name__)
        self._pending_fusion_title = None

    def _model_progress(self) -> None:
        # A native ACP prompt can already be streaming or waiting on a bridge tool before session/prompt returns.
        self._keep_first_prompt()
        self._start_parallel_fusion_title()

    def _start_parallel_fusion_title(self) -> None:
        candidate, fusion = self._pending_fusion_title, self._fusion
        if (candidate is None or fusion is None or fusion.registry.policy.max_tokens or not self.history_ready
                or self._close_requested or self._turn is None or self._turn.stopping):
            return
        self._pending_fusion_title = None
        self._fusion_title_task = self._spawn(self._generate_fusion_title(candidate, self.running_model))

    def _cancel_fusion_title(self) -> asyncio.Task[None] | None:
        task = self._fusion_title_task
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            return task
        return None

    async def _generate_fusion_title(self, candidate: tuple[str, Any, Callable[[], bool]], model: str) -> None:
        from desktop.core.fusion.title import generate
        try:
            question, account, allowed = candidate
            await generate(self, question, model, account, allowed)
        except Exception as error:
            log.info("Could not save a Fusion title: %s", type(error).__name__)

    async def _connection_prompt(self, connections, text: str) -> str:
        if connections is None:
            return ''
        if not self._connection_started:
            self._connection_context = await connections.hooks('SessionStart', self.project, {
                'session_id': self.session_id, 'source': 'resume' if self.saved.items else 'startup'})
            self._connection_started = True
        extra = await connections.hooks('UserPromptSubmit', self.project, {'session_id': self.session_id, 'prompt': text})
        return wrap_context(self._connection_context, extra)

    async def _run_turn(self, turn: Turn, sent: str, attachments: Sequence[Attachment], *, packed_connections: bool = False) -> None:
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
            if not packed_connections:
                sent += await self._connection_prompt(tools.connections, turn.user_block.text if turn.user_block else sent)
            item = user_item(sent, attachments)
            notes: list[str] = []
            sections: dict[str, str] = {}
            instructions = build_instructions(
                self.project,
                model=model,
                level=self.autonomy,
                servers=tools.servers(),
                notes=notes,
                plan=plan,
                skills=found,
                think_chinese=self.manager.settings.think_chinese,
                answer_language=self.manager.settings.answer_language,
                thinking_language=self.manager.settings.thinking_language,
                sections=sections,
                include_project_rules=self._include_project_rules(),
            )
            if self._fusion is not None:
                fusion_instructions = self._fusion.instructions()
                sections["fusion"] = fusion_instructions
                instructions += "\n\n" + fusion_instructions
                if tools.topic_declarer is not None:
                    from desktop.core.fusion import debate_bridge
                    sections["debate"] = debate_bridge.DECLARE_TOPIC_GUIDANCE
                    instructions += "\n\n" + debate_bridge.DECLARE_TOPIC_GUIDANCE
            snapshot = self.saved.prompt
            if not snapshot.get("instructions"):
                snapshot.update(instructions=instructions, current=instructions, sections=sections)
                if any(old.get("wc_context_update") for old in compact.view_of(self.saved.items, self.saved.compaction)):
                    self.saved.items.append(context_refresh())
            else:
                update = changed_context(snapshot["sections"], sections) if isinstance(snapshot.get("sections"), dict) else (
                    context_update(instructions) if snapshot.get("current") != instructions else None
                )
                if update is not None:
                    self.saved.items.append(update)
                snapshot.update(current=instructions, sections=sections)
            instructions = snapshot["instructions"]
            self._turn_start = len(self.saved.items)
            self.saved.items.append(item)
            if model.startswith("factory:"):
                self._unsent_prompt = turn.id  # May still be refused by the machine guard.
            declaration = self._declare_state() or {}
            kind = declaration.get('kind', '') if declaration.get('intent') == 'execute' else ''
            source = declaration.get('resume_source', '')
            self.saved.turns.append(SavedTurn(self._turn_start, time.time(), None, "running", "", model,
                                              fusion_plan=self._fusion._unit_key() if source else '',
                                              fusion_resumed=source == 'automatic',
                                              fusion_task=(declaration.get('task_id') or uuid.uuid4().hex) if kind else '',
                                              fusion_kind=kind, fusion_resume_source=source))
            if self.saved.plan.get("status") == "approved" and self.saved.plan.get("execution") == "starting":
                self.saved.plan["execution"] = "started"
            self.saved.model, self.saved.reasoning, self.saved.autonomy = self.model, self.reasoning, self.autonomy
            self.saved.mode = PLAN if self.mode == PLAN else ""
            if self._checkpoint_written or len(self.saved.turns) > 1:
                await self._save_durable(required=True)
            if self._fusion is not None:
                await self._fusion.sample_metrics(turn, start=True)
                await self._fusion.retain_plan_route()
            if tuple(notes) != self._prompt_notes:
                self._prompt_notes = tuple(notes)
                for note in notes:
                    self.transcript.add_notice(note)
            loop = AgentLoop(
                self._stream_turn,
                tools,
                _Watch(self, tools, turn, instructions=instructions, candidate=self._fusion is not None),
                model=model,
                reasoning=reasoning or None,
                instructions=instructions,
                shape=self.manager.shape,
                conversation=self.session_id,
                max_steps=self.turn_step_limit,
                reconnect_interval=self.manager.settings.reconnect_interval,
                reconnect_limit=self.manager.settings.reconnect_limit,
                max_continuations=2,
                **self._carried_measure(model),
            )
            # The loop works on the view (what the model reads: the summary of the early part, then the rest); what it adds
            # reaches the log (``saved.items``) through ``_Watch.history_grew``.
            native_run = None
            if model.startswith(GUARDED):
                if self._fusion is None and self._fusion_parent is None:
                    raise ChatGPTError("ACP 工具桥只用于 Fusion 会话。", code="fusion_acp")
                if model.startswith("factory:"):
                    from functools import partial
                    from desktop.core.fusion.factory_seat import run
                    native_run = partial(run, self)
                else:
                    from desktop.core.fusion.acp import AcpRun
                    self._acp = self._acp or AcpRun(self)
                    native_run = self._acp.run
                end = await native_run(tools, loop.observer, compact.view_of(self.saved.items, self.saved.compaction), sent, attachments, snapshot.get("current", instructions))
                self._keep_first_prompt()
            else:
                end = await loop.run(compact.view_of(self.saved.items, self.saved.compaction))
            fusion = self._fusion
            verdict = fusion.acceptance_verdict() if fusion is not None and not plan else None
            unrated = fusion.unrated_records() if fusion is not None else ()
            if fusion is not None and (verdict is not None or unrated):
                # One diagnostic pass is always possible. Further passes require new retained facts.
                seen = set()
                while end.reason == 'done' and end.steps < self.turn_step_limit():
                    continuation = verdict.continuation() if verdict is not None and verdict.missing else ''
                    asks = [continuation] if continuation else []
                    if unrated:
                        asks.append(fusion.rating_request(unrated))
                        unrated = ()
                    if not asks:
                        break
                    _, bound = fusion.recovery_plan()
                    stamp = fusion_recovery.progress_stamp(bound) if bound is not None and verdict and verdict.unit_plan else ''
                    if stamp in seen:
                        break
                    seen.add(stamp)
                    # Actual user-facing progress is not hidden just because checks still have to run.
                    if verdict is not None and verdict.missing:
                        loop.observer.preserve_answer()
                        held = False
                    else:
                        held = loop.observer.hold_answer()
                    continuation = "\n\n".join(asks) + (MERGE_CLAUSE if held else
                        " Then end with the answer to the user request, not with a report of these steps.")
                    loop.observer.history_grew([{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": continuation}]}])
                    await self._save_durable(required=True)
                    history = compact.view_of(self.saved.items, self.saved.compaction)
                    if native_run is not None:
                        end = await native_run(tools, loop.observer, history, continuation, (),
                                                  snapshot.get("current", instructions), steps_used=end.steps)
                    else:
                        end = await loop.run(history, steps_used=end.steps)
                    verdict = fusion.acceptance_verdict()
                loop.observer.release_answer()
                if verdict is not None:
                    await fusion.retain_verdict(verdict)
                await fusion.settle_main_ratings()
            else:
                # Nothing more owed: the turn settled on its own — the last words are the answer.
                loop.observer.release_answer()
            if tools.connections is not None and end.reason != "observer":
                try:
                    await tools.connections.hooks("Stop", self.project, {"session_id": self.session_id, "stop_hook_active": True})
                except ExtensionError:
                    # This hook runs after the answer; a veto must not reclassify completed actions as failed.
                    self.stop_hook_blocked = True
                    self.transcript.add_notice("Stop Hook 未通过；模型回合已完成，未自动重做。请检查 Hook 或继续给出要求。")
            if self._fusion is not None:
                await self._fusion.sample_metrics(turn)
        except asyncio.CancelledError:
            if self._turn is turn:
                if self._fusion is not None:
                    await self._fusion.sample_metrics(turn)
                if loop is not None and self._fusion is not None and self._fusion.acceptance_verdict() is not None:
                    loop.observer.release_answer()
                note = self._fusion_turn_receipt(turn, 'interrupted' if self._close_requested else 'cancelled')
                if note:
                    self.transcript.add_notice(note)
                self._release_steers()
                self.transcript.abort_turn("本轮已停止。")
                self._record_turn("interrupted", note=note)
                await self._end_turn(turn)
            raise
        except Exception as error:  # the session must survive anything
            failure = error
        finally:
            if loop is not None and (self._turn is None or self._turn is turn):  # (not while a later turn runs: this loop was left behind)
                self._keep_measure(loop, model)
        if self._turn is not turn:
            return
        if failure is not None:
            if loop is not None and loop.observer.defer_retry(failure) and hasattr(loop, 'completed_steps'):
                self.last_turn_end = TurnEnd('retry_after', steps=loop.completed_steps)
            if loop is not None and self._fusion is not None and self._fusion.acceptance_verdict() is not None:
                loop.observer.release_answer()
            await self._turn_failed(turn, failure)
            return
        assert end is not None
        self.last_turn_end = end
        if end.reason == "observer":
            self._release_steers()
            self.transcript.abort_turn("Observer 已暂停此专家。")
            self._record_turn("interrupted")
            await self._end_turn(turn)
            return
        if end.reason == "plan":
            previous = self.saved.plan
            self.mode = PLAN  # A mode change made during planning cannot approve or hide the submitted plan.
            self.saved.plan = {"id": uuid.uuid4().hex, "text": self._proposed_plan, "status": "pending"}
            # Bind the turn's validated work plan to this version id; approving it carries the same work plan.
            receipt, workplan = self._declare_state(), None
            if isinstance(receipt, dict):
                workplan = receipt.get("workplan")
            before_origin = self.saved.origin
            if isinstance(workplan, Mapping):
                self.saved.origin = {**self.saved.origin, "fusionWorkPlan": {"plan": self.saved.plan["id"],
                                                                             "workplan": dict(workplan)}}
            elif "fusionWorkPlan" in self.saved.origin:
                self.saved.origin = {key: value for key, value in self.saved.origin.items() if key != "fusionWorkPlan"}
            record = self.saved.turns[-1] if self.saved.turns else None
            before = (record.finished, record.status, record.error, record.note) if record is not None else None
            self._record_turn("done")
            try:
                await self._save_durable(required=True)
            except ChatGPTError as error:
                self.saved.plan = previous
                self.saved.origin = before_origin
                if record is not None and before is not None:
                    record.finished, record.status, record.error, record.note = before
                await self._turn_failed(turn, error)
                return
        note = ""
        if end.reason == "incomplete":
            note = STOP_NOTES.get(end.detail, f"回答没有说完（{end.detail}）。")
        elif end.reason == "step_limit":
            note = f"这一轮用了 {end.steps} 步还没结束，先停在这里；可在设置 → 会话里调整「单轮步数上限」，或回复「继续」。"
        receipt_note = self._fusion_turn_receipt(turn, end.reason)
        if receipt_note:
            note = receipt_note + ('\n' + note if note and end.reason != 'step_limit' else '')
        elif verdict is not None and (verdict.missing or verdict.unit_results):
            # Persist the program verdict like other stop notes, so a reopened chat shows it once.
            if verdict.unit_results:
                heading = 'Fusion：本轮工作未验证。' if verdict.missing else 'Fusion：本轮工作已验证。'
                result = '\n'.join((heading, *verdict.unit_results))
            else:
                result = ('Fusion：本轮工作未验证；缺少检查：' + ', '.join(verdict.missing) +
                          '。本轮已结束；须继续补齐检查后才能结案。')
                turn.fusion_outcome = 'unverified'
                if self.saved.turns:
                    self.saved.turns[-1].fusion_outcome = turn.fusion_outcome
            note = result + ('\n' + note if note else '')
        if note:
            self.transcript.add_notice(note)
        self._release_steers()
        pending = turn.fusion_outcome in ('waiting_user', 'unverified')
        self.transcript.finish_turn(turn, TurnStatus.INTERRUPTED if pending else TurnStatus.DONE)
        self._record_turn('interrupted' if pending else {"step_limit": "limit", "incomplete": "incomplete"}.get(end.reason, "done"), note=note)
        try:
            if self._pending_fusion_title is not None and self._fusion is not None and end.reason in ("done", "plan"):
                self._keep_first_prompt()
                if self._fusion.registry.policy.max_tokens:
                    # With a finite budget, keep the original reservation: the next turn waits for title usage.
                    self._fusion.finish_metrics(turn)
                    await self._generate_fusion_title(self._pending_fusion_title, model)
                else:
                    self._start_parallel_fusion_title()  # A successful prompt need not emit a progress event.
        finally:
            await self._end_turn(turn)
            self.refresh_context()

    def _checkpoint_dispatch(self) -> None:
        if not self._checkpoint_written:
            # Prepare first, then checkpoint just before entering the transport. A crash or disconnect after this
            # point is ambiguous and must not silently erase a possibly delivered prompt.
            self.saved.origin[FIRST_PROMPT] = True
            try:
                self._save_now(required=True)
            except BaseException:
                self.saved.origin.pop(FIRST_PROMPT, None)
                raise

    async def _stream_turn(self, request: dict[str, Any]) -> AsyncIterator[Event]:
        self._checkpoint_dispatch()
        self._check_fusion_title_source()
        started = False
        try:
            async with contextlib.aclosing(self.manager.stream(request)) as events:
                async for event in events:
                    if isinstance(event, Started):
                        started = True
                    else:
                        self._keep_first_prompt()
                    yield event
        except Exception as error:
            if self.saved.origin.get(FIRST_PROMPT) is True and self._request_refused(error, started=started):
                self._discard_first_prompt()
            else:
                self._keep_first_prompt()
            raise

    @staticmethod
    def _request_refused(error: Exception, *, started: bool) -> bool:
        if isinstance(error, (AuthError, RequestNotSent, ValueError)):
            return True
        if not isinstance(error, ChatGPTError):
            return False
        if error.status is not None:
            return 400 <= error.status < 500 and error.status != 408
        if error.code == "network":
            # HttpClient reports a pre-send connection failure as network, but a possibly sent body as stream.
            return not started
        return error.code == "api" or (isinstance(error.body, dict) and error.code not in ("stream", "stream_idle", "timeout"))

    def _discard_first_prompt(self) -> None:
        if self._checkpoint is not None:
            self._finish_checkpoint(self._checkpoint)
        try:
            self.manager.sessions.discard_first_prompt(self.session_id)
        except OSError:
            # On a storage failure prefer leaving the recovery checkpoint to rewriting it as an empty history.
            log.warning("could not roll back the rejected first prompt", exc_info=True)
        self.saved.origin.pop(FIRST_PROMPT, None)
        self._checkpoint_written = False

    def _keep_first_prompt(self) -> None:
        if self.saved.origin.pop(FIRST_PROMPT, None) is True:
            self._first_prompt_retained = True
            self._notify("history")

    async def _turn_failed(self, turn: Turn, error: BaseException) -> None:
        if isinstance(error, ChatGPTError):
            self._recovery_delay = error.retry_after
            if self._fusion is not None and (key := self._fusion._unit_key()):
                self._fusion.defer_external(key, None, self._recovery_delay)
        if self._fusion_parent is not None:
            if isinstance(error, TimeoutError):
                self.check_retry_reason = "timeout"
            elif isinstance(error, ChatGPTError) and not isinstance(error, (AuthError, RequestNotSent)) and not error.retry_after:
                if error.code in ("timeout", "stream_idle"):
                    self.check_retry_reason = "timeout"
                elif is_transient(error) or error.code in ("stream", "fusion_acp_no_report", "fusion_factory_no_report"):
                    self.check_retry_reason = "incomplete run"
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
        acp = self.saved.origin.get("fusionAcp")
        if (isinstance(error, EngineRpcError) and error.code in (-32600, -32601, -32602)
                and isinstance(acp, dict) and self.saved.origin.get(FIRST_PROMPT) is True
                and len(self.saved.items) == self._turn_start + 1):
            self._discard_first_prompt()
            acp["promptAttempted"] = False
        self._keep_first_prompt()  # an ACP dispatch may have reached the native agent even when it did not answer
        note = self._fusion_turn_receipt(turn, 'failed')
        if note:
            self.transcript.add_notice(note)
        if (not note and not self._first_prompt_retained and (not self.running_model.startswith("factory:") or self._unsent_prompt == turn.id)
                and len(self.saved.items) == self._turn_start + 1 and self.saved.turns
                and self.saved.turns[-1].start == self._turn_start and self.saved.turns[-1].finished is None):
            # The message was rejected without output: the next try must not repeat it from history.
            del self.saved.items[self._turn_start :]
            self.saved.turns.pop()
            if not self._checkpoint_written:
                self.saved.prompt = {}
        else:
            self._record_turn("failed", message, note=note)
        self._release_steers()
        self.transcript.fail_turn(message)
        await self._end_turn(turn)

    def _fusion_turn_receipt(self, turn: Turn, reason: str) -> str:
        rt = self._fusion
        record = self.saved.turns[-1] if self.saved.turns else None
        if rt is None or record is None or record.start != self._turn_start:
            return ''
        if not record.fusion_plan:
            if reason == 'cancelled' and record.fusion_task:
                turn.fusion_outcome = record.fusion_outcome = 'cancelled'
            verdict = rt.acceptance_verdict() if reason in ('failed', 'cancelled', 'interrupted') else None
            if verdict is not None and verdict.missing:
                turn.fusion_outcome = record.fusion_outcome = 'cancelled' if reason == 'cancelled' else 'unverified'
                return ('Fusion：本轮工作未验证。尚缺：' + '、'.join(verdict.missing) +
                        '。本轮中断，已保留已知结果；须核对现有证据，不能据此结案。')
            return ''
        plan = rt.registry.plans.get(record.fusion_plan)
        if not plan or not plan['cases']:
            return ''
        missing = [c for c in plan['cases'].values() if c['next'] != 'done']
        main = plan.get('mainRecovery', {})
        waiting = any(verification_case.awaiting_user(c) for c in (*missing, main))
        delayed = any(fusion_recovery.waiting(c) for c in (*missing, main))
        outcome = 'cancelled' if reason == 'cancelled' else 'waiting_user' if waiting else 'unverified' if missing or delayed else 'verified'
        turn.fusion_outcome = record.fusion_outcome = outcome
        if outcome == 'cancelled':
            try:
                rt.cancel_recovery(record.fusion_plan)
            except ChatGPTError:
                log.warning('Could not checkpoint the Fusion stop', exc_info=True)
        record.fusion_recovery = (outcome == 'unverified' and not self.stop_hook_blocked and reason in ('step_limit', 'incomplete')
                                  and not any(t.fusion_plan == record.fusion_plan and t.fusion_outcome == 'unverified'
                                              for t in self.saved.turns[:-1]))
        if outcome == 'verified':
            lines, _ = verification_case.result_lines(plan)
            return '\n'.join(('Fusion：本轮工作已验证。', *lines))
        cause = {'cancelled': '收到停止指令', 'failed': '运行异常', 'step_limit': '本轮步数已用尽', 'incomplete': '模型未完成回答',
                 'observer': 'Observer 暂停', 'done': '模型回合已结束，验收尚未通过'}.get(reason, '本轮未通过验收')
        action = ('已停止执行；尚未通过的验收不计为完成。' if outcome == 'cancelled' else
                  '请回答续作卡。已授权的其他工单不受此问题限制。' if waiting else
                  '正在等待服务允许重试；到时从检查点继续，等待期间不发模型请求。' if delayed else
                  '将从已保存的检查点继续原任务，不重复未知副作用。' if record.fusion_recovery else
                  '保留检查点；需继续诊断未通过的检查，不能据此结案。')
        lines, _ = verification_case.result_lines(plan)
        return '\n'.join(('Fusion：本轮工作未验证。', *lines, f'本轮离开原因：{cause}。', action))

    def _record_turn(self, status: str, error: str = "", note: str = "") -> None:
        if self.saved.turns and self.saved.turns[-1].start == self._turn_start and self.saved.turns[-1].finished is None:
            record = self.saved.turns[-1]
            record.finished, record.status, record.error, record.note = time.time(), status, error, note

    def _schedule_fusion_recovery(self) -> None:
        if self._fusion is not None and self.state is SessionState.READY and not self._close_requested:
            asyncio.get_running_loop().call_soon(self._service_fusion_recovery)

    def _service_fusion_recovery(self) -> None:
        batch, rt = self._fusion_resume_batch, self._fusion
        if batch is None:
            self._resume_fusion_checkpoint()
            return
        if rt is None or self.state is not SessionState.READY or self._close_requested:
            return
        if self.mode == PLAN or self.stop_hook_blocked:
            self._fusion_resume_batch = None
            self.transcript.add_notice('Fusion：已停止启动其余续作，原任务仍保留。')
            self._changed()
            return
        if batch.active is not None:
            # Give the current task its existing automatic recovery opportunity before starting another one.
            self._resume_fusion_checkpoint()
            if self.state is not SessionState.READY or self._fusion_resume_focus is not None:
                return
            plan = rt.registry.plans.get(batch.active.plan)
            if plan is not None and not plan.get('cancelled') and any(
                verification_case.awaiting_user(owner) or fusion_recovery.waiting(owner)
                for _, owner in rt._permission_owners(plan)
            ):
                batch.waiting.append(batch.active)
            batch.active = None
        try:
            while batch.pending:
                target = self._current_resume_target(batch.pending.pop(0))
                if target is None:
                    continue  # no longer an eligible original task
                if target.plan and not rt.unit_tools_available:
                    self.transcript.add_notice(f'Fusion：{target.label} 的冻结工单当前不可用，未改走旧式派工。')
                    continue
                if target.plan and not fusion_recovery.can_start_manual(rt.registry.plans[target.plan]):
                    batch.waiting.append(target)
                    continue
                if self.send_prompt(batch.text, resume_target=target):
                    batch.active = target
                    return
                break
            else:
                while batch.waiting:
                    target = self._current_resume_target(batch.waiting.pop(0))
                    if target is None or not rt.unit_tools_available:
                        continue
                    batch.active = target
                    self._focus_fusion_resume(target)
                    self._resume_fusion_checkpoint()
                    return
        except ChatGPTError:
            self.transcript.add_notice('Fusion：续作未能启动，已保留原任务。')
        self._fusion_resume_batch = None
        self._changed()

    def _resume_fusion_checkpoint(self) -> None:
        if (self.state is not SessionState.READY or self._close_requested or self.mode == PLAN or self._fusion is None
                or self._fusion_resume_question is not None):
            return
        rt = self._fusion
        key, plan = rt.recovery_plan()
        last = self.saved.turns[-1] if self.saved.turns else None
        if plan is None or last is None or (last.fusion_plan == key and last.fusion_outcome == 'cancelled') or self.stop_hook_blocked:
            return
        now = time.time()
        try:
            rt.wake_external(plan, now=now)
        except ChatGPTError:
            return
        owners = rt._permission_owners(plan)
        deadlines = [owner['externalWait']['until'] for _, owner in owners if fusion_recovery.waiting(owner)]
        if self._recovery_timer is not None:
            self._recovery_timer.cancel()
            self._recovery_timer = None
        if deadlines:
            self._recovery_timer = asyncio.get_running_loop().call_later(max(0, min(deadlines) - now), self._resume_fusion_checkpoint)
        main = plan.get('mainRecovery', {})
        if fusion_recovery.waiting(main) or verification_case.awaiting_user(main) and not fusion_recovery.runnable(plan):
            return
        signals = [(name, case, case['resumeSignal']) for name, case in owners if case.get('resumeSignal')]
        ready = [(name, owner, owner['externalWait']) for name, owner in owners
                 if (owner.get('externalWait') or {}).get('status') == 'ready']
        runnable = fusion_recovery.runnable(plan)
        if not runnable and not any(name is None for name, _, _ in (*signals, *ready)):
            return
        focus_row = self.saved.origin.get(fusion_recovery.RESUME_FOCUS) if self._fusion_resume_focus else None
        armed = bool(focus_row and focus_row.get('armed'))
        if not armed and not (last.fusion_plan == key and last.fusion_recovery) and not signals and not ready:
            return
        details = []
        for name, case, signal in signals:
            if signal.get('requestKind') == 'recoveryRequest':
                request = case['recoveryRequest']
                result = ('reported doing the requested operation; verify it before work' if signal['approved'] else
                          'cannot provide this prerequisite; choose an authorized alternative')
                details.append(f"{name}: the user {result}. Prerequisite: {request['question']}. No new tool permission granted.")
            elif signal.get('requestKind') == 'toolPermission':
                action = case['permissions'][signal['id']]['display']
                result = 'approved once' if signal['approved'] else 'declined; pursue a different authorized action'
                details.append(f"{name or 'Main'}: {action['tool_name']} was {result}. Approval is scoped to the exact original input.")
            else:
                result = 'approved the specified budget increase' if signal['approved'] else 'did not approve the increase; stay within authorization'
                details.append(f'{name}: the user {result}.')
        details.extend(f"{name or 'Main'}: service retry delay elapsed; recheck authorization and budget before continuing."
                       for name, _, _ in ready)
        original = next((record for record in reversed(self.saved.turns) if record.fusion_plan == key), last)
        target = self._fusion_resume_focus or fusion_recovery.ResumeTarget(fusion_recovery.task_id(original), 'complex', key)
        note = self._resume_notice(target, details)
        # Keep a restart marker on the previous turn until a newer turn exists. A process failure after this
        # checkpoint but before dispatch still recovers the task; no timer or user click must be repeated.
        prior = last.fusion_recovery
        if focus_row:
            self.saved.origin[fusion_recovery.RESUME_FOCUS] = {**focus_row, 'armed': True}
        else:
            last.fusion_recovery = True
        for _, case, _ in signals:
            case.pop('resumeSignal')
        for _, owner, wait in ready:
            owner['externalWait'] = {**wait, 'status': 'released'}
        try:
            rt._checkpoint()
        except ChatGPTError:
            last.fusion_recovery = prior
            if focus_row:
                self.saved.origin[fusion_recovery.RESUME_FOCUS] = focus_row
            for _, case, signal in signals:
                case['resumeSignal'] = signal
            for _, owner, wait in ready:
                owner['externalWait'] = wait
            return
        if not self.send_prompt(note, recovery=True, resume_target=target):
            last.fusion_recovery = prior
            if focus_row:
                self.saved.origin[fusion_recovery.RESUME_FOCUS] = focus_row
            for _, case, signal in signals:
                case['resumeSignal'] = signal
            for _, owner, wait in ready:
                owner['externalWait'] = wait
            with contextlib.suppress(ChatGPTError):
                rt._checkpoint()

    async def _end_turn(self, turn: Turn) -> None:
        if self._turn is not turn:
            return
        self._pending_fusion_title = None
        self._keep_first_prompt()  # stopped after dispatch, or finished without a separate response event
        if ((not self._checkpoint_written or self._unsent_prompt == turn.id) and self.saved.turns
                and self.saved.turns[-1].start == self._turn_start):
            # Stopped during local setup, before a request could reach the model.
            del self.saved.items[self._turn_start :]
            self.saved.turns.pop()
            if not self._checkpoint_written:
                self.saved.prompt = {}
        if self._fusion is not None:
            self._fusion.finish_metrics(turn)
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
        self._proposed_plan = ""
        self.active_at = time.time()
        self.refresh_context()  # (a model chosen during the turn now gives its window)
        try:
            await self._save_durable()
        finally:
            if self.state is SessionState.RUNNING:
                self.state = SessionState.READY
            if self._turn_done is not None:
                self._turn_done.set()
            if self._acp is not None and self._fusion_parent is None:
                self._acp.arm_idle()
            self._notify("state")
            self._changed()
            self._schedule_fusion_recovery()

    # ------------------------------------------------------------------ steering and stopping

    async def steer(self, text: str, attachments: Sequence[Attachment] = ()) -> SteerOutcome:
        """Say something to the turn that is running: the model reads it at its next request."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        turn = self._turn
        if self.state is not SessionState.RUNNING or turn is None or turn.status is not TurnStatus.RUNNING:
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

    def cancel_waiting_fusion(self) -> bool:
        if self._fusion is None or self.state is not SessionState.READY:
            return False
        if self._fusion_resume_question is not None:
            self._fusion_resume_question = None
            self._changed()
            return True
        batch = self._fusion_resume_batch
        self._fusion_resume_batch = None
        key, plan = self._fusion.recovery_plan()
        if plan is None or not any(verification_case.awaiting_user(owner) or fusion_recovery.waiting(owner)
                                   for _, owner in self._fusion._permission_owners(plan)):
            if batch is not None:
                self._changed()
            return batch is not None
        if not self._fusion.cancel_recovery(key):
            return False
        record = self.saved.turns[-1]
        turn = self.transcript.turns[-1]
        message = 'Fusion：已取消原任务；未完成的验收不计为通过。'
        if record.fusion_plan == key:
            record.fusion_outcome = turn.fusion_outcome = 'cancelled'
            record.fusion_recovery = False
            record.note += '\n' + message
        self._fusion_resume_focus = None
        self.saved.origin.pop(fusion_recovery.RESUME_FOCUS, None)
        self.transcript.add_notice(message)
        self._save_now()
        self._changed()
        return True

    async def interrupt(self) -> None:
        turn, task = self._turn, self._turn_task
        if self.state is not SessionState.RUNNING or turn is None:
            return
        self._fusion_resume_batch = None
        self.transcript.mark_stopping()
        self._cancel_fusion_title()
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
        if self._turn is turn and turn.status is TurnStatus.RUNNING:
            log.warning("the turn did not end within %.0f s of the stop request", INTERRUPT_GRACE_SECONDS)
            note = self._fusion_turn_receipt(turn, 'interrupted' if self._close_requested else 'cancelled')
            if note:
                self.transcript.add_notice(note)
            self._release_steers()  # (the loop never took them: back to the user, not into the next turn)
            self.transcript.abort_turn("停止超时：这里先停止显示。")
            self._record_turn("interrupted", note=note)
            # The loop of this turn is still winding down but no longer owns the conversation (``_Watch`` drops what it
            # reports). Its calls get their outputs here: the next turn must not send a history with a call that has none.
            pair_in_place(self.saved, INTERRUPTED)
            await self._end_turn(turn)

    # ------------------------------------------------------------------ command: /compact

    async def compact_for_switch(self, window: int = 0) -> str:
        """Finish a source-model summary before changing model; cancellation/failure never changes the model."""
        if not self.saved.items:
            return (self.handoff.summary or self.handoff.prefix) if self.handoff is not None and not self.handoff.empty else ""
        if self.state is not SessionState.READY or self._turn is not None:
            raise ChatGPTError("会话尚未就绪，未切换模型。", code="compaction")
        record = self.saved.compaction
        if (record and record.get("upto") == len(self.saved.items) and record.get("summary")
                and (not window or compact.estimate_items(compact.view_of(self.saved.items, record)) <= compact.budget(window))):
            return str(record["summary"])
        # A handoff is part of sending, not a synthetic user command. Keep the ownership needed by _compact without
        # creating, completing or saving an independent /compact turn (or invoking prompt/stop hooks).
        turn = Turn(id=self.transcript.new_id("switch"))
        previous_task = self._turn_task
        self._turn, self._turn_task = turn, asyncio.current_task()
        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode
        self._turn_start = len(self.saved.items)
        self.state = SessionState.RUNNING
        self._notify("state")
        try:
            view = compact.view_of(self.saved.items, self.saved.compaction)
            done = await self._compact(turn, view, compact.estimate_items(view), False, manual=True,
                                       for_switch=True, switch_window=window)
            record = self.saved.compaction
            if not done or not record or record.get("upto") != len(self.saved.items):
                raise ChatGPTError("交接摘要未能覆盖历史或放入目标窗口；原模型和历史保留，消息未发送。", code="compaction")
            return str(record["summary"])
        finally:
            self._turn, self._turn_task = None, previous_task
            self._turn_model = self._turn_reasoning = self._turn_mode = ""
            if self.state is SessionState.RUNNING:
                self.state = SessionState.READY
            self.transcript.set_working_state(None)
            self._notify("state")
            self._changed()

    def start_compaction(self, *, for_switch: bool = False, window: int = 0) -> str | None:
        """``/compact``: turn the conversation so far into a summary now. It runs as a turn of its own (the page shows the command,
        the working state and the result; the stop button works) that adds nothing to the conversation but the summary.
        Why it cannot start (a sentence for the user), or ``None`` when it has started."""
        if self.state is not SessionState.READY:
            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"
        if self.model.startswith("devin:") and not for_switch:
            return None if self.send_prompt("/compact") else "ACP 会话暂不能压缩。"
        if not self.saved.items:
            return "还没有对话可以压缩。"
        self.used_at = time.time()
        self.state = SessionState.RUNNING
        self.stop_hook_blocked = False
        self.last_turn_end = None
        self.check_retry_reason = ""
        self._recovery_delay = None
        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode
        self._turn_start = len(self.saved.items)  # even setup failures belong to a new boundary, never the previous turn
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn("/compact")
        self._turn = turn
        self._turn_done = asyncio.Event()
        self.transcript.set_working_state("compacting_conversation")
        self._turn_task = self._spawn(self._run_compaction(turn, for_switch=for_switch, window=window))
        self._notify("state")
        self._changed()
        return None

    async def _run_compaction(self, turn: Turn, *, for_switch: bool = False, window: int = 0) -> None:
        failure: BaseException | None = None
        done = False
        try:
            view = compact.view_of(self.saved.items, self.saved.compaction)
            done = await self._compact(turn, view, compact.estimate_items(view), False, manual=True,
                                       for_switch=for_switch, switch_window=window)
        except asyncio.CancelledError:
            if self._turn is turn:
                self.transcript.abort_turn("压缩已停止，对话没有改动。")
                await self._end_turn(turn)
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
        await self._end_turn(turn)

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

    def _add_usage(self, usage: Usage, *, measures_context: bool = True, counts_speed: bool = True, model: str = "") -> None:
        tokens = usage.tokens()
        self._usage_total = tokens if self._usage_total is None else self._usage_total.plus(tokens)
        if counts_speed:
            self._speed_output += tokens.output_tokens
        model = model or self.running_model
        if model:
            self._usage_models[model] = self._usage_models.get(model, Tokens()).plus(tokens)
            self.saved.usage_models = {name: value.to_json() for name, value in self._usage_models.items()}
        total = self._usage_total
        self.saved.usage = total.to_json()
        if not measures_context:  # Summary and title requests say nothing about the conversation's context size.
            self._changed()
            return
        self.saved.context_used = usage.context
        model = self.running_model
        window = self.manager.model_window(model) or self.context.window
        self._publish_context(replace(self.context, used=usage.context, window=window, model=model, compacted=False))
        self._changed()

    # ------------------------------------------------------------------ keeping inside the window

    def _economic_window(self, window: int | None) -> int | None:
        """Only documented, direct API billing: do not impose an API surcharge policy on subscriptions/gateways."""
        from urllib.parse import urlsplit
        from desktop.core.llm.model_specs import long_context_threshold

        api = getattr(self.manager, "api", None)
        spec = api.spec(self.running_model) if api is not None and hasattr(api, "spec") else None
        if spec is None or urlsplit(spec.base_url).hostname != "api.openai.com":
            return window
        threshold = long_context_threshold(spec.native)
        return min(window, threshold) if window and threshold else window

    def _summary_effort(self) -> str | None:
        """The lowest reasoning level of the model: a summary needs no deep thought, and the user's own level may be Max."""
        info = self.manager.model_info(self.running_model)
        efforts = tuple(getattr(info, "efforts", ()) or ())
        return "low" if "low" in efforts else (efforts[0] if efforts else None)

    async def _compact(
        self, owner: Turn, history: list[dict[str, Any]], estimate: int, overflow: bool, fixed: int = 0, *, must_fit: bool = False, manual: bool = False,
        for_switch: bool = False, switch_window: int = 0,
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
        if not for_switch and self._tools is not None and self.saved.prompt.get("current"):
            fixed = compact.fixed_tokens(self.saved.prompt["current"], self._tools.declarations(), self.manager.shape)
        # (refused although the estimate said it fits: aim well below what was refused; no window known: below the estimate)
        target = min(window, int(estimate * 0.7)) if window and overflow else (window or int(estimate * 0.7))
        if manual:
            target = window or MANUAL_WINDOW
        if for_switch and switch_window:
            target = min(target, switch_window)
        target = self._economic_window(target) or target
        record = self.saved.compaction
        # (the loop's view is view_of(log, record): it begins with a summary because there is a record, never because of what
        # a message says, which may be a summary the user pasted)
        synthetic = compact.synthetic_count(record)
        start = int(record.get("upto") or 0) if record else 0
        self.transcript.set_working_state("compacting_conversation")
        self._changed()
        prefix = None
        if not for_switch and self._tools is not None and self.saved.prompt.get("instructions") and not model.startswith(("api:", *GUARDED)):
            prefix = build_request(
                model=model, instructions=self.saved.prompt["instructions"], items=[], tools=self._tools.declarations(),
                reasoning=(self._turn_reasoning if self._turn_model else self.reasoning) or None, shape=self.manager.shape, conversation=self.session_id,
            )
        def used(usage: Usage) -> None:
            if self._turn is owner:
                self._add_usage(usage, measures_context=False)
        fusion = self._fusion or getattr(self._fusion_parent, "_fusion", None)
        continuity = fusion.context_snapshot(expert_id=self.session_id if self._fusion_parent is not None else "") if fusion is not None else ""
        if not continuity and self.saved.plan.get("status") == "approved":
            continuity = json.dumps({"approved_plan": self.saved.plan.get("text", "")}, ensure_ascii=False)
        def requested(request: dict[str, Any], response: Finished | None, seconds: float, failed: bool) -> None:
            if self._turn is owner:
                if fusion is not None:
                    fusion.metric_summary_request(self.session_id, response is not None and response.usage is not None)
                    fusion.record_request(response, failed)
                entry = self._trace.record(request, response, seconds, failed)
                entry["kind"] = "summary"
                self.saved.requests.append(entry)
                del self.saved.requests[:-MAX_RECORDS]
        from desktop.core.fusion.summary import seat_stream
        stream = seat_stream(self, model)
        async def summary_stream(request: dict[str, Any]):
            if fusion is not None:
                estimate = compact.estimate_items(request.get("input") or ()) + compact.estimate_text(str(request.get("instructions") or ""))
                try:
                    fusion.check_budget(estimate)
                except ValueError as error:
                    raise ChatGPTError("Fusion 预算不足或用量未知，已暂停摘要请求。", code="fusion_budget") from error
            async with contextlib.aclosing(stream(request)) as events:
                async for event in events:
                    yield event
        pending: PendingSnapshot | None = None
        if self._archive_pending and not self._archive_broken:
            # A compaction that precedes the first checkpoint opens the archive itself rather than run unarchived.
            self._archive_begin()
        binding = self._archive_binding
        if self._archive_pending or self._archive_broken or binding is not None:
            # A session that archives must not compact without it: stage first, and only a staged snapshot,
            # a finished summary and a committed publish together may swap the view and ``saved.compaction``.
            if binding is None or self._archive_broken:
                raise ChatGPTError("会话归档不可用，未进行压缩；原始历史完整保留。请检查归档目录或存储设置后重试。", code="archive")
            self._archive_sync()
            if self._archive_broken:
                raise ChatGPTError("会话归档同步失败，未进行压缩；原始历史完整保留。请检查归档目录或存储设置后重试。", code="archive")
            try:
                pending = binding.prepare(history, start, synthetic)
            except Exception as error:
                raise ChatGPTError("会话归档暂存失败，未进行摘要；原始历史完整保留。请检查存储空间或归档目录。", code="archive") from error

        def drop() -> None:
            nonlocal pending
            if pending is not None and binding is not None:
                try:
                    binding.abort(pending)
                except Exception:
                    log.warning("could not drop the staged archive snapshot of %s", self.session_id, exc_info=True)
                pending = None

        try:
            result = await compact.compact(
                history,
                stream=summary_stream if fusion is not None else stream,
                model=model,
                reasoning=self._summary_effort(),
                window=target,
                fixed=fixed,
                synthetic=synthetic,
                previous=str(record.get("summary") or "") if record else "",
                kept=compact.kept_of(record) if record else "",
                turn=len(history) if manual else synthetic + self._turn_start - start,
                on_usage=used,
                whole=manual,
                handoff=for_switch,
                fallback=False,
                continuity=continuity,
                rebase=True,
                output_reader=lambda reference: asyncio.to_thread(self._outputs.read, reference),
                prefix_request=prefix,
                on_request=requested,
            )
        except compact.CompactionFailed as error:
            drop()
            raise ChatGPTError("摘要未完成，已暂停；原始历史和执行证据仍保留，没有用用户请求清单替代。可以重试压缩或换更大窗口的模型。", code="compaction") from error
        except BaseException:
            drop()
            raise
        finally:
            if self._turn is owner:
                self.transcript.set_working_state("thinking")
                self._changed()
        if self._turn is not owner:
            drop()
            return False
        entry: dict[str, Any] | None = None
        # A short conversation may have a slightly longer summary. Switching still needs a semantic handoff,
        # not raw replay; accept it only when it fits the destination window.
        if result is not None and (result.fits or (for_switch and result.after + fixed <= compact.budget(target))):
            upto = compact.log_index(result.cut, start, synthetic)
            entry = {
                "upto": upto, "summary": result.summary, "kept": result.kept, "at": time.time(), "before": result.before,
                "after": result.after, "model": model, "mechanical": result.mechanical,
            }
            if continuity:
                entry["continuity"] = continuity
            if ((for_switch and upto != len(self.saved.items))
                    or not compact.boundary_ok(self.saved.items, upto, after_turn=for_switch)
                    or compact.view_of(self.saved.items, entry) != result.view):
                # Do not publish a partial handoff or a view that does not match the durable log.
                log.error("a compaction of the conversation %s does not match its log (upto %d); not kept", self.session_id, upto)
                entry = None
        if entry is None:
            drop()
            if must_fit:
                raise self._too_big(model, window, result, estimate, fixed, overflow)
            return False
        previous_used = self.saved.context_used
        self.saved.compaction = entry
        self.saved.context_used = 0
        try:
            if pending is not None and binding is not None:
                # The companion session save joins the archive's commit window as its checkpoint: the
                # archive lock is held inside it, so it must save without syncing again. A failed save
                # rolls the generation back — ``latest``, the staged file and this record all stay as before.
                binding.publish(pending, entry,
                                checkpoint=lambda _generation: self._save_now(required=True, already_synced=True))
                pending = None
            elif for_switch:
                self._save_now(required=True)
        except BaseException as error:
            self.saved.compaction, self.saved.context_used = record, previous_used
            drop()
            if isinstance(error, Exception) and not isinstance(error, ChatGPTError):
                raise ChatGPTError("摘要已生成，但归档提交失败；压缩未生效，原始历史完整保留。", code="archive") from error
            raise
        if not for_switch and self.saved.prompt.get("current"):
            self.saved.prompt["instructions"] = self.saved.prompt["current"]
        history[:] = result.view
        fusion = self._fusion or getattr(self._fusion_parent, "_fusion", None)
        if fusion is not None:
            fusion.metric_compaction(self.session_id)
        self._publish_context(replace(self.context, used=None, compacted=True))
        notice = ("已为模型切换压缩上下文；完整历史和执行证据仍保留。" if for_switch
                  else self._compaction_notice(result, model, window, overflow, manual))
        self.transcript.add_notice(notice)
        log.info("compacted the conversation %s: ~%d -> ~%d tokens%s", self.session_id, result.before, result.after, " (digest only)" if result.mechanical else "")
        if not for_switch:
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
        elif window and self._economic_window(window) != window:
            text = f"对话约 {before}，按「{name}」的长上下文价格分界提前压缩到约 {after}，减少后续请求的 Token 开销"
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

    def _snapshot(self, *, pair: bool = True) -> SavedSession:
        saved = self.saved
        saved.project = str(self.project)
        saved.title = self.title if self.title != "新会话" else saved.title
        saved.model, saved.reasoning, saved.autonomy = self.model, self.reasoning, self.autonomy
        saved.mode = PLAN if self.mode == PLAN else ""
        saved.modified_at = self.active_at
        saved.recalled = sorted(self.recalled)
        # (a call still waiting or running has no output yet; the file must be a history the next request could send
        # even if the real output never gets written. The turns and the compaction follow their items.)
        return paired(saved) if pair else saved

    def _save_soon(self) -> None:
        if self._save_handle is not None or not self.saved.turns or self._close_requested:
            return

        def launch() -> None:
            self._save_handle = None
            if not self._close_requested:
                self._spawn(self._save_durable())

        with contextlib.suppress(RuntimeError):
            self._save_handle = asyncio.get_running_loop().call_later(SAVE_DELAY_SECONDS, launch)

    def _prepare_save(self, *, title_only: bool = False) -> bool | None:
        if title_only and not self._checkpoint_written:
            return False  # The side request must never create the conversation.
        if not title_only and self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if not self._checkpoint_written and FIRST_PROMPT not in self.saved.origin:
            acp = self.saved.origin.get("fusionAcp")
            if self._turn is not None and isinstance(acp, dict) and acp.get("promptAttempted") is True:
                # The ACP bridge sets this only at its durable dispatch boundary.
                self.saved.origin[FIRST_PROMPT] = True
            elif not (any(t.finished is not None for t in self.saved.turns) or self.saved.goal and not self.transcript.turns):
                return True  # settings, Fusion configuration and local turn setup are not archives
        if not title_only and self._archive_pending and not self._archive_broken:
            self._archive_begin()
        return None

    def _save_error(self, error: OSError, *, title_only: bool = False) -> str:
        if title_only:
            log.warning("could not checkpoint the optional title", exc_info=error)
            return ""
        if not self._checkpoint_written:
            self.saved.origin.pop(FIRST_PROMPT, None)
        log.warning("could not save the conversation", exc_info=error)
        message = "会话保存失败，请检查存储空间和权限；未保存的内容在退出后可能丢失，后续请求和写操作已暂停。"
        if not self._save_failed:
            self._save_failed = True
            self.transcript.add_notice(message)
            self._changed()
        return message

    def _finish_checkpoint(self, pending: _Checkpoint) -> None:
        if pending.settled:
            return
        try:
            pending.writing.result()
        except OSError as error:
            pending.error = error
            self._save_error(error)
        else:
            self._checkpoint_written = True
            self._save_failed = False
            if pending.archive is not None:
                pending.archive()
        finally:
            pending.settled = True
            if self._checkpoint is pending:
                self._checkpoint = None

    async def _wait_checkpoint(self, pending: _Checkpoint) -> None:
        waiting = asyncio.wrap_future(pending.writing)
        try:
            await asyncio.shield(waiting)
        except asyncio.CancelledError:
            # A worker holding the writer lease may not outlive cancellation/close or be overtaken by a rollback.
            with contextlib.suppress(OSError):
                await asyncio.shield(waiting)
            self._finish_checkpoint(pending)
            raise
        except OSError:
            pass  # publish the failure exactly once on the event loop below
        self._finish_checkpoint(pending)

    async def _save_durable(self, *, required: bool = False) -> bool:
        """Freeze state on the owner thread; await full encoding, index and atomic fsync in an ordered worker."""
        while self._checkpoint is not None:
            await self._wait_checkpoint(self._checkpoint)
        if self.state is SessionState.CLOSED:
            return False
        decision = self._prepare_save()
        if decision is not None:
            return decision
        snapshot = deepcopy(self._snapshot(pair=False))
        segment = None if self._archive_binding is None or (self._turn and self._unsent_prompt == self._turn.id) else self._archive_segment()
        archive = (lambda: self._archive_sync(snapshot=snapshot, segment=segment)) if segment is not None else None
        store = self.manager.sessions
        pending = self._checkpoint = _Checkpoint(_CHECKPOINT_WRITERS.submit(lambda: store.save(paired(snapshot))), archive)
        await self._wait_checkpoint(pending)
        if pending.error is not None and required:
            # The notice precedes this error, which must still stop the caller's side effect or next request.
            raise ChatGPTError("会话保存失败，后续请求和写操作已暂停。", code="checkpoint") from pending.error
        return pending.error is None

    def _save_now(self, *, required: bool = False, title_only: bool = False, already_synced: bool = False) -> bool:
        """For synchronous state transactions only; never let a pending older write overwrite this one."""
        if self._checkpoint is not None:
            self._finish_checkpoint(self._checkpoint)
        decision = self._prepare_save(title_only=title_only)
        if decision is not None:
            return decision
        try:
            self.manager.sessions.save(self._snapshot())
        except OSError as error:
            message = self._save_error(error, title_only=title_only)
            if required and not title_only:
                raise ChatGPTError(message, code="checkpoint") from error
            return False
        self._checkpoint_written = True
        self._save_failed = False
        if not title_only and not already_synced:
            self._archive_sync()
        return True

    def _archive_adopt(self, binding: ArchiveBinding) -> None:
        """Bind the session archive and move this session's tool-output store into it — one store per logical
        session (``<sid>/output``), for both a freshly created archive and a restored one."""
        self._archive_binding = binding
        self._outputs = binding.archive.outputs
        if self._context is not None:
            self._context.outputs = self._outputs

    def _archive_fail(self) -> None:
        """Mark the archive broken and say so once: the conversation keeps saving, but ``_compact`` refuses to
        run unarchived rather than silently degrading its evidence."""
        first = not self._archive_broken
        self._archive_broken = True
        log.warning("the session archive of %s failed", self.session_id, exc_info=True)
        if first:
            self.transcript.add_notice("会话归档写入失败：原文证据未继续归档，压缩已被阻止；请检查存储设置与磁盘空间。")
            self._changed()

    def _archive_begin(self) -> None:
        """A conversation opened by ``open_new`` opens its session archive at its first durable checkpoint;
        the sid joins ``saved.origin`` in the same write, so a reopen restores exactly this archive. A failed
        create retries at the next checkpoint and blocks compaction meanwhile — never archives-disabled-then-compact."""
        try:
            self._archive_adopt(ArchiveBinding.create(self.manager, self.session_id))
        except Exception:
            self._archive_fail()
            return
        self._archive_pending = False
        self._archive_broken = False
        self.saved.origin[ARCHIVE_ORIGIN] = self._archive_binding.archive.sid

    def _archive_segment(self) -> tuple[str, str, str] | None:
        """``(backend, connection, native id)`` the current stretch of the log is archived under, by the actual
        conversation backend — ``gpt`` on the ChatGPT plan, ``wittyharness`` on API providers (the harness is the
        conversation), ``fusion`` on Fusion routing. The ACP stretch's native id comes only from the real
        handshake (``_acp.session`` / the persisted ``saved.origin['fusionAcp']['session']``) — before one
        exists nothing may be written, so ``None`` means "not yet archivable". A Fusion session on a local
        driver is still ``fusion`` (its ``_fusion`` state, not a model prefix) and uses our session id. Local
        backends sharing that id and driver get separate connection keys, so a switch creates a new archive
        instance and a switch back resumes the old one."""
        model = self.running_model or self.model or ""
        if model.startswith("devin:"):
            acp = getattr(self._acp, "session", "")
            origin = self.saved.origin.get("fusionAcp")
            saved_native = origin.get("session") if isinstance(origin, dict) else ""
            native = acp or (saved_native if isinstance(saved_native, str) else "")
            return ("fusion", "devin", native) if native else None
        if model.startswith("factory:"):
            return ("fusion", "factory", self.session_id)  # The guarded session is host-owned, never resumed by CLI id.
        driver = "api" if model.startswith("api:") else "gpt"
        backend = "fusion" if self._fusion is not None else ("wittyharness" if driver == "api" else "gpt")
        binding = self._archive_binding
        connection = binding.local_connection(backend, driver) if binding is not None else driver
        return backend, connection, self.session_id

    def _archive_before_handoff(self) -> None:
        """Fusion ACP knows its real native session id and is about to write retained handoff text: bind
        the archive now so those writes land in the session-scoped store. An archive failure stops the
        handoff; a session that never had an archive stays unarchived (no migration)."""
        if self._archive_binding is None:
            if self._archive_pending and not self._archive_broken:
                self._archive_begin()
            if self._archive_binding is None:
                if self._archive_pending or self._archive_broken:
                    raise ChatGPTError("会话归档不可用，未进行交接；原始历史完整保留。请检查归档目录或存储设置后重试。", code="archive")
                return
        self._archive_sync()
        if self._archive_broken:
            raise ChatGPTError("会话归档写入失败，未进行交接；原始历史完整保留。请检查归档目录或存储设置后重试。", code="archive")

    def _archive_sync(self, *, snapshot: SavedSession | None = None, segment: tuple[str, str, str] | None = None) -> None:
        """Append what was durable in this checkpoint, never live state that grew while the worker wrote."""
        binding = self._archive_binding
        if binding is None or (snapshot is None and self._turn and self._unsent_prompt == self._turn.id):
            return  # Do not append an unsent, still reversible user item to an immutable archive.
        try:
            if segment is None:
                segment = self._archive_segment()
            if segment is None:
                return  # the ACP stretch has no real native id yet; nothing is written until the handshake
            backend, connection, native = segment
            binding.ensure_instance(backend, native, connection)
            durable = snapshot if snapshot is not None else self.saved
            binding.sync(durable.items, durable.turns)
        except Exception:
            self._archive_fail()
        else:
            self._archive_broken = False

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
        if self._fusion is not None and isinstance(prompt_id, str) and prompt_id.startswith('fusion-tool-'):
            if not self._fusion.answer_tool_permission(prompt_id, value):
                return False
            self._schedule_fusion_recovery()
            return True
        future = self._pending.get(prompt_id)
        if future is None or not self.transcript.answer_permission(prompt_id, value):
            return False
        if not future.done():
            future.set_result(value)
        return True

    def answer_question(self, prompt_id: str, answers: Sequence[str | Sequence[str]] | None) -> bool:
        """Answer (or, with ``None``, skip) the question the model asked with ``ask_user``; each answer is a string or a list of
        strings."""
        if isinstance(prompt_id, str) and prompt_id.startswith('fusion-resume-'):
            question = self._fusion_resume_question
            if (question is None or question[0] != prompt_id or self._fusion is None
                    or self.state is not SessionState.READY or self.mode == PLAN or self._close_requested):
                return False
            _, text, targets = question
            options = self._resume_options(targets)
            if answers is None:
                self._fusion_resume_question = None
                self._changed()
                return True
            if len(answers) != 1 or answers[0] not in options:
                return False
            self._fusion_resume_question = None
            if answers[0] == '全部续作':
                self._fusion_resume_focus = None
                self._fusion_resume_batch = _ResumeBatch(text, list(targets))
                self._changed()
                self._schedule_fusion_recovery()
                return True
            if not self.send_prompt(text, resume_target=targets[options.index(answers[0])]):
                self._changed()
                return False
            return True
        if self._fusion is not None and isinstance(prompt_id, str) and prompt_id.startswith('fusion-'):
            if not self._fusion.answer_recovery(prompt_id, answers):
                return False
            self._schedule_fusion_recovery()
            return True
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

    def __init__(self, controller: ChatGPTController, tools: HarnessTools, owner: Turn, *, instructions: str = "",
                 candidate: bool = False) -> None:
        self.ctl = controller
        self.tools = tools
        self.owner = owner
        self.instructions = instructions
        """The turn's instructions: with the tool declarations, what every request holds besides the conversation."""
        self._planning_outputs: tuple[list[str], bool] = ([], False)
        self._compacted_to = 0
        """The estimate right after the last compaction of this turn (or at the last one that could not help)."""
        self._staging = Staging() if candidate else None
        """A Fusion main turn's answer text stays buffered until the step's fate is known (fusion.candidate)."""
        self._flagged: dict[str, dict[str, Any]] = {}
        """Log items marked ``CANDIDATE`` while still buffered, by staged id — the mark comes off at publication."""
        self._answer_pass = 0

    @property
    def current(self) -> bool:
        return self.ctl._turn is self.owner

    @property
    def _turn(self) -> Turn | None:
        return self.ctl._live() if self.current else None

    def working(self, state: str | None) -> None:
        if not self.current:
            return
        if state == "thinking" and self._staging is not None:
            # A new request begins: the previous request's unflushed text was mid-turn progress after all.
            self._release_staged()
            self._staging.begin()
        self.ctl.transcript.set_working_state({"running_tool": "executing_tool"}.get(state or "", state))
        self.ctl._changed()

    def _release_staged(self) -> None:
        """Buffered answer text that turned out mid-turn: publish it now, in the order it was written."""
        if self._staging is None or not self._staging.staged():
            return
        turn = self._turn
        published: list[str] = []
        for item_id, text, done in self._staging.flush():
            if turn is None:
                continue  # Without the turn it never renders; keep the mark so the reopened view agrees.
            published.append(item_id)
            if done:
                self.ctl.transcript.end_text(turn, self._text_key(item_id), 0, text)
            else:
                self.ctl.transcript.append_text(turn, self._text_key(item_id), 0, text)
        if turn is not None:
            self.ctl._changed()
            if unflag(self._flagged, published):
                self.ctl._save_soon()  # The CANDIDATE mark comes off with the same write the publication reaches.

    def begin_step(self) -> None:
        if self._staging is not None:
            self._staging.begin()

    def hold_answer(self) -> bool:
        """Keep the staged answer internal: the continuation the caller runs next supersedes it (the log keeps it)."""
        if self._staging is None:
            return False
        held = self._staging.drop()
        self._flagged.clear()  # Forget the ids: a reused id in the next run must never unmark the superseded draft.
        return held

    def release_answer(self) -> None:
        self._release_staged()

    def preserve_answer(self) -> None:
        self._release_staged()
        self._answer_pass += 1  # Native providers may recycle the same message id on continuation.

    def _text_key(self, item_id: str) -> str:
        return f'fusion-{self._answer_pass}:{item_id}' if self._answer_pass else item_id

    def text_delta(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            if self._staging is None or not self._staging.delta(item_id, text):
                self.ctl.transcript.append_text(self._turn, self._text_key(item_id), 0, text)
                self.ctl._changed()
            if text.strip():
                self.ctl._model_progress()

    def text_done(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            if self._staging is None or not self._staging.done(item_id, text):
                self.ctl.transcript.end_text(self._turn, self._text_key(item_id), 0, text)
                self.ctl._changed()
            if text.strip():
                self.ctl._model_progress()

    def thinking_delta(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self._release_staged()  # Thinking comes after the answer text it follows: that text was progress.
            self.ctl.transcript.append_thinking(self._turn, item_id, 0, text)
            if text.strip():
                self.ctl._model_progress()
            self.ctl._changed()

    def thinking_done(self, item_id: str, text: str) -> None:
        if self._turn is not None:
            self._release_staged()
            self.ctl.transcript.end_thinking(self._turn, item_id, 0, text=text or None)
            if text.strip():
                self.ctl._model_progress()
            self.ctl._changed()

    def call_begun(self, item_id: str, call_id: str, name: str) -> None:
        if self._turn is not None:
            if self._staging is not None:
                self._staging.calls = True
            self._release_staged()  # The text before a tool call is mid-turn progress, not the answer.
            ui_name, _ = self.tools.display(name, {})
            self.ctl.transcript.ensure_tool(self._turn, call_id, ui_name)
            self.ctl._model_progress()
            self.ctl._changed()

    def call_ready(self, call: Call) -> None:
        turn = self._turn
        if turn is None:
            return
        self._release_staged()
        ui_name, data = self.tools.display(call.name, call.args)
        tool = self.ctl.transcript.ensure_tool(turn, call.call_id, ui_name)
        self.ctl.transcript.set_tool_input(turn, tool, data, final=True)
        self.ctl._model_progress()
        self.ctl._changed()

    async def ask(self, call: Call, verdict: Verdict) -> str:
        if not self.current:
            return 'cancel'
        if self.tools.recovery_ask is not None:
            answer = self.tools.recovery_ask(call, verdict)
            if answer is not None:
                return answer
        return await self.ctl._ask(call)

    async def call_running(self, call: Call) -> None:
        if not self.current:
            return
        spec = self.tools.registry.get(call.name)
        if spec is None or spec.kind != "read" or self.tools.connections is not None:
            await self.ctl._save_durable(required=True)  # Failure must prevent the tool and its hooks from running.
        turn = self._turn
        tool = turn.tools.get(call.call_id) if turn is not None else None
        if turn is not None and tool is not None:
            self.ctl.transcript.tool_running(turn, tool)
            self.ctl._changed()

    async def retain_output(self, call: Call, outcome: Outcome) -> None:
        from desktop.core.harness.outputs import RETAIN_CHARS, UNRETAINED
        if not self.current or call.name in UNRETAINED or outcome.reference or len(outcome.output) <= RETAIN_CHARS:
            return
        work = asyncio.create_task(asyncio.to_thread(self.ctl._outputs.put, outcome.output))
        try:
            outcome.reference = await asyncio.shield(work)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                outcome.reference = await work
            raise
        except (OSError, ToolError) as error:
            raise ChatGPTError("工具已结束，但完整输出未能保存，已暂停以免丢失执行证据。请检查空间、权限或缩小输出范围。", code="output_storage") from error

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
            self._release_staged()
            self.ctl.transcript.add_notice(text)
            self.ctl._changed()

    def usage(self, usage: Usage) -> None:
        if self.current:
            self.ctl._add_usage(usage)

    def request_done(self, request: dict[str, Any], finished: Finished | None, seconds: float, failed: bool) -> None:
        if self.current:
            if self._staging is not None:
                if failed:
                    fusion_main = self.ctl._fusion
                    if fusion_main is not None and (fusion_main._unit_key() or fusion_main.acceptance_verdict() is not None):
                        self.preserve_answer()  # The request failed, but the received body belongs to this recovery.
                    else:
                        self._staging.drop()
                        self._flagged.clear()
                elif self._staging.calls:
                    # A request that made calls ends a step that continues: its text is progress, show it.
                    self._release_staged()
            fusion = self.ctl._fusion or getattr(self.ctl._fusion_parent, "_fusion", None)
            if fusion is not None:
                usage = finished.usage if finished is not None else None
                if self.ctl._fusion is not None:
                    names, mixed = self._planning_outputs
                    fusion.metric_planning_reply(names, mixed or failed, usage)
                    self._planning_outputs = ([], False)
                fusion.metric_request(self.ctl.session_id, usage is not None, usage.input_tokens if usage is not None else None)
                fusion.record_request(finished, failed)
            self.ctl.saved.requests.append(self.ctl._trace.record(request, finished, seconds, failed))
            del self.ctl.saved.requests[:-MAX_RECORDS]
            self.ctl._save_soon()

    def history_grew(self, items: Sequence[dict[str, Any]]) -> None:
        if self.current:
            # The own-loop emits the current reply's items immediately before request_done. Never scan the archive
            # or split a shared response bill according to text length. Retain names only, never request content.
            if self.ctl._fusion is not None and any(i.get('type') == 'function_call' for i in items):
                self._planning_outputs = ([i.get('name', '') for i in items if i.get('type') == 'function_call'],
                                          any(i.get('type') not in ('function_call', 'reasoning') for i in items))
            if self._staging is not None and self._staging.staged():
                # Still buffered as they are saved: marked now, unmarked the moment their text is published.
                self._flagged.update(mark_candidates(items, self._staging))
            self.ctl.saved.items.extend(items)  # (the loop works on the view; the log is what the page and the file are made of)
            if any(item.get("wc_images") for item in items):
                prune_tool_images(self.ctl.saved.items)  # Include the part of the log that is no longer in the view.
            self.ctl._save_soon()

    def fixed(self) -> int:
        """Tokens of what every request of the turn holds besides the conversation."""
        return compact.fixed_tokens(self.instructions, self.tools.declarations(), self.ctl.manager.shape)

    def _compact_at(self, window: int) -> int:
        """Where the user wants the conversation compacted: their percent of the window (read at every request)."""
        settings = self.ctl.manager.settings
        identity = self.ctl._fusion_identity  # (a Fusion identity file may set its own point: design section 7)
        percent = identity.compact_at if identity is not None and identity.compact_at else settings.chatgpt_compact_at
        return int((self.ctl._economic_window(window) or window) * percent / 100)

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
        return max(0, self._compact_at(window) - 1 - estimate, compact.keep_tokens(self.ctl._economic_window(window) or window) - step)

    def retry_allowed(self) -> bool:
        fusion = self.ctl._fusion or getattr(self.ctl._fusion_parent, "_fusion", None)
        return fusion is None or not fusion.registry.policy.max_tokens or not fusion.registry.unmetered

    async def _fusion_context(self) -> tuple[str, dict[str, str], list[str], Any]:
        ctl = self.ctl
        found = await asyncio.to_thread(skills.discover, ctl.project)
        settings = ctl.manager.settings
        sections: dict[str, str] = {}
        notes: list[str] = []
        current = await asyncio.to_thread(
            build_instructions, ctl.project, model=ctl.running_model, level=ctl.autonomy, servers=self.tools.servers(),
            notes=notes, plan=ctl._turn_mode == PLAN, skills=found, think_chinese=settings.think_chinese,
            answer_language=settings.answer_language, thinking_language=settings.thinking_language, sections=sections,
            include_project_rules=ctl._include_project_rules(),
        )
        if ctl._fusion is not None:
            own = ctl._fusion.instructions()
            sections["fusion"] = own
            current += "\n\n" + own
        return current, sections, notes, found

    async def before_tools(self) -> bool:
        fusion = self.ctl._fusion or getattr(self.ctl._fusion_parent, "_fusion", None)
        if fusion is None:
            return True
        _, sections, _, _ = await self._fusion_context()
        previous = self.ctl.saved.prompt.get("sections")
        return self.current and isinstance(previous, dict) and changed_context(previous, sections) is None

    async def _refresh_fusion_context(self, history: list[dict[str, Any]]) -> int:
        ctl = self.ctl
        current, sections, notes, found = await self._fusion_context()
        snapshot = ctl.saved.prompt
        previous = snapshot.get("sections")
        update = changed_context(previous, sections) if isinstance(previous, dict) else None
        if update is None or not self.current:
            return 0
        if ctl._context is not None:
            ctl._context.readable = skills.read_roots(found)
        snapshot.update(current=current, sections=sections)
        history.append(update)
        ctl.saved.items.append(update)
        await ctl._save_durable(required=True)
        if tuple(notes) != ctl._prompt_notes:
            ctl._prompt_notes = tuple(notes)
            for note in notes:
                ctl.transcript.add_notice(note)
        ctl._changed()
        return compact.estimate_item(update)

    async def prepare(self, history: list[dict[str, Any]], estimate: int, overflow: bool) -> bool:
        if not self.current:
            return False
        fusion = self.ctl._fusion or getattr(self.ctl._fusion_parent, "_fusion", None)
        if self.ctl._save_failed:
            await self.ctl._save_durable(required=True)
        if fusion is not None:
            estimate += await self._refresh_fusion_context(history)
            try:
                fusion.check_budget(estimate)
            except ValueError as error:
                raise ChatGPTError("Fusion 预算不足或用量未知，已暂停后续请求；请检查预算及模型用量。", code="fusion_budget") from error
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
        if done:
            self.instructions = self.ctl.saved.prompt.get("instructions", self.instructions)
            fixed = self.fixed()
        # (after one that could not help too: it is not tried again before the conversation has grown)
        self._compacted_to = compact.estimate_items(history) + fixed if done else estimate
        return done

    def defer_retry(self, error: BaseException) -> bool:
        return (self.current and isinstance(error, ChatGPTError) and error.retry_after is not None and
                (self.tools.recovery_pause is not None or self.ctl._fusion is not None and bool(self.ctl._fusion._unit_key())))

    def finish_requested(self) -> str:
        if not self.current:
            return ""
        if self.ctl._turn_mode == PLAN and self.ctl._proposed_plan:
            return "plan"
        if self.tools.recovery_pause is not None and self.tools.recovery_pause():
            return 'permission'
        fusion = getattr(self.ctl._fusion_parent, "_fusion", None)
        observed = fusion.observer_runs.get(self.ctl.session_id) if fusion is not None else None
        if observed is not None and observed.report is not None:
            return "observer"
        reporting = fusion.report_runs.get(self.ctl.session_id) if fusion is not None else None
        return "report" if reporting is not None and reporting.accepted is not None else ""

    def has_steer(self) -> bool:
        return self.current and bool(self.ctl._steers)

    def take_steer(self) -> list[Steer]:
        if not self.current:
            return []
        taken, self.ctl._steers = self.ctl._steers, []
        if taken:
            self._release_staged()  # The answer goes on: what was written so far is progress.
        return taken

    def steer_given(self, steer: Steer) -> None:
        turn = self._turn
        if turn is not None:
            self.ctl.transcript.settle_steer(turn, steer.ref)
            self.ctl._changed()


__all__ = ["DEFAULT_LEVEL", "ChatGPTController", "normalize_level", "user_item"]
