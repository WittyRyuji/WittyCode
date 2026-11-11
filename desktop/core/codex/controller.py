"""One Codex thread as an open session, with the public surface of ``SessionController`` (``core/session_manager``).

The engine (``engine.CodexEngine``) owns the one app-server process that all threads share and hands each controller
the notifications and server requests of its thread (all of them carry ``threadId``). The controller turns them into
the transcript, through the ``Transcript`` builder API only (``items`` says which item becomes what).

Lifecycle (``SessionState``, the same enum as Droid's sessions, so the backend treats both alike):

- ``open_new``: ``thread/start`` (cwd, model, approvalPolicy, sandbox) -> READY. ``open_saved``: history
  (``thread/turns/list``) -> ``thread/resume`` -> READY; the thread's own model, effort and sandbox become the settings.
- ``send_prompt``: RUNNING -> ``turn/start`` (input, cwd, approvalPolicy, sandboxPolicy, model, effort; what a turn
  sets stays the thread's setting) ... ``turn/completed`` -> READY. ``update_settings`` only records a change: it is
  sent with the next turn (the stable API has no call that changes an idle thread's settings).
- ``interrupt``: ``turn/interrupt``, then the turn ends with ``turn/completed`` (``interrupted``). codex-cli 0.165.0 never
  answers an interrupt for a turn that has already ended (verified), so that request has a timeout, and a turn that has
  not ended ``INTERRUPT_GRACE_SECONDS`` after the stop is ended here (shown as stopped).
- The process dying: DEAD plus a recoverable error block, for every Codex session at once (they share the process);
  ``reconnect``: a new process, ``thread/resume`` with this controller's settings, READY.

Handoff: ``open_new(..., transcript=, handoff=)`` continues a conversation another engine had; the first ``send_prompt``
sends ``handoff.prefix + text``, the transcript shows ``text`` with ``handoff=(carried, truncated)`` on its user block,
and the handoff is dropped once that turn has finished DONE (kept for the retry otherwise), as ``SessionController`` does.

Usage and context come from ``thread/tokenUsage/updated``: ``usage`` is the thread's running total (``CodexUsage``),
``context.used`` the last model call's ``totalTokens``, ``context.window`` its ``modelContextWindow``.
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
from desktop.core.codex.errors import (
    CodexError,
    CodexProcessError,
    CodexRpcError,
    CodexTimeoutError,
    friendly_failure,
)
from desktop.core.codex.elicit import plan_of, reply_for
from desktop.core.codex.history import build_turns
from desktop.core.codex.rollout import read_rollout
from desktop.core.codex.items import (
    EXECUTE,
    TEXT_ITEMS,
    TODO,
    command_action,
    file_action,
    file_changes,
    file_diffs,
    permissions_action,
    plan_todos,
    reasoning_text,
    retry_text,
    split_handoff,
    tool_outcome,
    tool_spec,
    turn_error_text,
    turn_input,
)
from desktop.core.codex.policy import (
    APPROVAL_OPTIONS,
    DEFAULT_LEVEL,
    EXPIRED_DECISION,
    decision_for,
    level_of,
    normalize_level,
    permission,
    sandbox_policy,
    sandbox_type,
)
from desktop.core.context import ContextState
from desktop.core.context_envelope import wrap as wrap_context
from desktop.core.handoff import Handoff
from desktop.core.hooks import PromptEvent
from desktop.core.memory.launch import new_run
from desktop.core.prompts import QuestionView
from desktop.core.session_manager import SESSION_STATE_LABELS, LiveSettings, SessionState, SteerOutcome
from desktop.core.toolinfo import first_line
from desktop.core.toolinfo import working_label as label_for_working_state
from desktop.core.transcript import PromptStatus, ToolItem, ToolStatus, Transcript, Turn, TurnStatus
from desktop.core.usage import Tokens, own_output

if TYPE_CHECKING:
    from desktop.core.codex.engine import CodexEngine

log = logging.getLogger("desktop.codex")

THREAD_OPEN_SECONDS = 120.0
"""thread/start and thread/resume also start the thread's MCP servers, which can take a while."""
TURN_START_SECONDS = 60.0
STEER_SECONDS = 15.0
STEER_WAIT_SECONDS = 5.0
"""How long a steer waits for ``turn/start`` to answer with the turn's id, which ``turn/steer`` must name."""
HISTORY_SECONDS = 60.0
INTERRUPT_GRACE_SECONDS = 8.0
OPEN_CLOSE_WAIT_SECONDS = 10.0
UNSUBSCRIBE_SECONDS = 5.0
HISTORY_PAGE = 50
HISTORY_MAX_TURNS = 400
OUTPUT_KEEP_CHARS = 64 * 1024
METHOD_NOT_FOUND = -32601
SERVICE_NAME = "wittycode"
WORKSPACE_DOWNGRADED = (
    "这台电脑上的 Codex 还没有设置 Windows 沙箱，「工作区」权限现在和「只读」一样：改文件和运行需要写权限的命令前都会先问你。"
)
TRANSPORT_NOISE = ("Falling back from WebSockets",)


@dataclass(frozen=True, slots=True)
class CodexUsage:
    """A thread's running token total, with the attribute names of Droid's ``TokenUsageUpdate``.

    ``input_tokens`` leaves out what came from the cache (and what was written to it), the page's In; Codex reports
    ``cachedInputTokens`` as part of ``inputTokens`` (OpenAI's usage). OpenAI models report no cache writes, so
    ``cache_creation_tokens`` is ``None`` (the page shows "—") unless the server counts some.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int | None = None
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_breakdown(cls, raw: Mapping[str, Any]) -> CodexUsage:
        def count(key: str) -> int:
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0

        cached, written = count("cachedInputTokens"), count("cacheWriteInputTokens")
        return cls(
            input_tokens=max(0, count("inputTokens") - cached - written),
            output_tokens=count("outputTokens"),
            cache_creation_tokens=written or None,
            cache_read_tokens=cached,
            thinking_tokens=count("reasoningOutputTokens"),
            total_tokens=count("totalTokens"),
        )


async def saved_turns(client: Any, thread_id: str | None) -> list[Mapping[str, Any]]:
    """A thread's saved turns, oldest first (at most the newest ``HISTORY_MAX_TURNS``)."""
    newest_first: list[Mapping[str, Any]] = []
    cursor: str | None = None
    try:
        while len(newest_first) < HISTORY_MAX_TURNS:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "itemsView": "full",
                "sortDirection": "desc",
                "limit": HISTORY_PAGE,
            }
            if cursor:
                params["cursor"] = cursor
            page = await client.request("thread/turns/list", params, timeout=HISTORY_SECONDS)
            data = [turn for turn in (page.get("data") if isinstance(page, Mapping) else None) or () if isinstance(turn, Mapping)]
            newest_first.extend(data)
            cursor = _text(page.get("nextCursor")) if isinstance(page, Mapping) else ""
            if not cursor or not data:
                break
    except CodexRpcError as error:
        if newest_first:
            return list(reversed(newest_first))
        # A thread with nothing saved yet answers -32601 "list_turns is not supported yet" (verified);
        # a thread kept in the older format may only offer the full read.
        try:
            result = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=HISTORY_SECONDS)
        except CodexRpcError as again:
            if METHOD_NOT_FOUND in (error.code, again.code):
                return []
            raise
        thread = result.get("thread") if isinstance(result, Mapping) else None
        turns = thread.get("turns") if isinstance(thread, Mapping) else None
        return [turn for turn in turns or () if isinstance(turn, Mapping)]
    return list(reversed(newest_first))


def last_call_tokens(last: Any) -> int | None:
    """How big the context was at the last model call: its input (cached included) plus its output."""
    if not isinstance(last, Mapping):
        return None
    total = last.get("totalTokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        return int(total)
    parts = [last.get(key) for key in ("inputTokens", "outputTokens")]
    value = sum(int(part) for part in parts if isinstance(part, (int, float)) and not isinstance(part, bool) and part > 0)
    return value or None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _answer_text(answer: str | Sequence[str]) -> str:
    return answer if isinstance(answer, str) else ", ".join(answer)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _thread_id(result: Any) -> str:
    thread = result.get("thread") if isinstance(result, Mapping) else None
    return _text(thread.get("id")) if isinstance(thread, Mapping) else ""


@dataclass(eq=False)
class CodexController:
    """Owns one Codex thread: its transcript, the running turn and the pending approvals."""

    manager: CodexEngine
    project: Path
    transcript: Transcript = field(default_factory=Transcript)
    state: SessionState = SessionState.OPENING
    session_id: str | None = None
    """The Codex thread id."""
    saved_title: str | None = None
    opened_at: float = field(default_factory=time.time)
    used_at: float = field(default_factory=time.time)
    active_at: float = field(default_factory=time.time)
    """When the conversation last had a message sent or answered (the sidebar's order and time); opening or resuming
    does not change it. Same meaning as Droid's controller."""
    last_error: str = ""
    eviction_notice: str = ""
    context: ContextState = field(default_factory=ContextState)
    handoff: Handoff | None = None
    requested_model: str = ""
    reasoning: str = ""
    """Codex's effort string (``low`` ... ``max``); empty: the model's default."""
    autonomy: str = DEFAULT_LEVEL
    """``readonly`` | ``workspace`` | ``full`` (``policy``)."""
    engine: ClassVar[str] = "codex"
    run: str = field(default_factory=new_run)
    """Tells the shared memory which conversation a note comes from (it is in the command of the memory server)."""
    preface: str = ""
    """The note about the shared memory put after the first message of a new thread; dropped once that turn is done."""
    _preface_turn: Turn | None = None
    recalled: set[str] = field(default_factory=set)
    """What the application's hooks gave this conversation with its messages (ids of notes): not given again."""
    _recall_turn: Turn | None = None
    _recall_ids: frozenset[str] = frozenset()
    _model: str = ""
    _usage: CodexUsage | None = None
    _client: Any = None
    _turn: Turn | None = None
    _turn_id: str | None = None
    _turn_done: asyncio.Event | None = None
    _handoff_turn: Turn | None = None
    _ended_turns: set[str] = field(default_factory=set)
    _interrupt_wanted: bool = False
    _needs_resume: bool = False
    _turn_error: Any = None
    _retry_note: str = ""
    _notes: list[str] = field(default_factory=list)
    _downgrade_noted: bool = False
    _outputs: dict[str, str] = field(default_factory=dict)
    _summary_index: dict[str, int] = field(default_factory=dict)
    _started_ms: dict[str, float] = field(default_factory=dict)
    _plans: int = 0
    _warnings: set[str] = field(default_factory=set)
    _pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    _requests: dict[Any, str] = field(default_factory=dict)
    _listeners: list[Callable[[CodexController, str], None]] = field(default_factory=list)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _close_requested: bool = False
    _close_task: asyncio.Task[None] | None = None
    _opening: asyncio.Future[None] | None = None
    agents: dict[str, CodexAgent] = field(default_factory=dict)
    """The sub-agents this thread has started (by their thread id), oldest first: Codex runs them, we show them."""

    # ------------------------------------------------------------------ plumbing

    def add_listener(self, callback: Callable[[CodexController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[CodexController, str], None]) -> None:
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
    def usage(self) -> CodexUsage | None:
        """The thread's running token total (``None`` before Codex reported any)."""
        return self._usage

    def usage_series(self) -> dict[str, Tokens]:
        """The thread's own total and each sub-agent's: separate threads, each counting from zero."""
        series = {} if self._usage is None else {"self": Tokens.of(self._usage)}
        for thread_id, agent in self.agents.items():
            counted = Tokens.of(agent.usage)
            if counted is not None:
                series[f"agent:{thread_id}"] = counted
        return series

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
        if self.agent_prompts():
            return "子任务等待你的确认"
        if self._retry_note:
            return self._retry_note
        return label_for_working_state(turn.working_state if turn is not None else None)

    def agent_prompts(self) -> list[tuple[CodexAgent, Any]]:
        """What the sub-agents are waiting for the user to answer: ``(agent, prompt)``."""
        return [(agent, prompt) for agent in self.agents.values() for prompt in agent.transcript.pending_prompts()]

    def answer_agent_permission(self, agent_id: str, prompt_id: str, value: str) -> bool:
        agent = self.agents.get(agent_id)
        return agent is not None and agent.answer_permission(prompt_id, value)

    def answer_agent_question(self, agent_id: str, prompt_id: str, answers: Sequence[str | Sequence[str]] | None) -> bool:
        agent = self.agents.get(agent_id)
        return agent is not None and agent.answer_question(prompt_id, answers)

    def _agent_changed(self, agent: CodexAgent) -> None:
        self._changed()

    def _on_agent_activity(self, item: Mapping[str, Any]) -> None:
        """The thread started a sub-agent (``subAgentActivity``): from now on its notifications and approvals are ours."""
        agent_id = _text(item.get("agentThreadId"))
        if not agent_id or agent_id == self.session_id or self._client is None:
            return
        agent = self.agents.get(agent_id)
        if agent is None:
            agent = CodexAgent(
                manager=self.manager,
                project=self.project,
                session_id=agent_id,
                parent=self,
                path=_text(item.get("agentPath")),
                agent_model=_text(item.get("model")),
                agent_effort=_text(item.get("reasoningEffort")),
            )
            agent._client = self._client
            agent.state = SessionState.READY
            self.agents[agent_id] = agent
            self.manager._attach(agent)
            self._spawn(agent.learn())
        else:
            agent.agent_model = _text(item.get("model")) or agent.agent_model
            agent.agent_effort = _text(item.get("reasoningEffort")) or agent.agent_effort
        self._changed()

    def _release_agents(self) -> None:
        """The thread is closing or lost: no sub-agent keeps waiting for an answer nobody can give."""
        for agent in self.agents.values():
            agent._expire_pending()
            self.manager._detach(agent)

    def _effort(self) -> str:
        if self.reasoning:
            return self.reasoning
        info = self.manager.model_info(self.model)
        return info.default_effort if info is not None else ""

    def live_settings(self) -> LiveSettings | None:
        """The settings the next turn runs with (Codex has no separate report of them between turns)."""
        if not self.session_id:
            return None
        return LiveSettings(model=self.model, reasoning=self._effort(), autonomy=self.autonomy, mode="auto")

    # ------------------------------------------------------------------ opening

    async def open_new(self) -> None:
        memory = self.manager.memory
        self.preface = memory.brief_for("codex", self.project) if memory is not None else ""
        await self._opened(self._start_thread())

    async def open_saved(self) -> None:
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

    def _thread_params(self) -> dict[str, Any]:
        rule = permission(self.autonomy)
        params: dict[str, Any] = {
            "cwd": str(self.project),
            "approvalPolicy": rule.approval,
            "sandbox": rule.sandbox,
            "serviceName": SERVICE_NAME,
        }
        if self.requested_model:
            params["model"] = self.requested_model
        params.update(self.manager.memory_params(self))
        return params

    def _resume_params(self) -> dict[str, Any]:
        rule = permission(self.autonomy)
        params: dict[str, Any] = {
            "threadId": self.session_id,
            "excludeTurns": True,
            "cwd": str(self.project),
            "approvalPolicy": rule.approval,
            "sandbox": rule.sandbox,
        }
        if self.model:
            params["model"] = self.model
        params.update(self.manager.memory_params(self))
        return params

    async def _start_thread(self) -> None:
        try:
            client = await self.manager.connection()
            await self.manager.ensure_signed_in(client)
            result = await client.request("thread/start", self._thread_params(), timeout=THREAD_OPEN_SECONDS)
            thread_id = _thread_id(result)
            if not thread_id:
                raise CodexError("thread/start returned no thread id")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._close_requested:
                return
            self.state = SessionState.CLOSED
            self.last_error = friendly_failure(error)
            self.transcript.add_error(self.last_error)
            self._notify("state")
            return
        if self._close_requested:
            await self._forget_thread(client, thread_id)  # close() ran meanwhile: nobody owns this thread now
            return
        self._attach(client, result, adopt=False)
        self.state = SessionState.READY
        self._notify("state")

    async def _resume_saved(self) -> None:
        try:
            client = await self.manager.connection()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(friendly_failure(error))
            return
        await self._load_history(client)
        if self._close_requested:
            return
        try:
            result = await client.request(
                "thread/resume",
                {"threadId": self.session_id, "excludeTurns": True, **self.manager.memory_params(self)},
                timeout=THREAD_OPEN_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(friendly_failure(error))
            return
        if self._close_requested:
            if self.session_id:
                await self._forget_thread(client, self.session_id)
            return
        self._attach(client, result, adopt=True)
        await self._learn_saved_usage(result)
        self.state = SessionState.READY
        self._notify("state")

    async def _learn_saved_usage(self, resumed: Any) -> None:
        """Codex reports token use only with a turn that runs, so a resumed thread would show none: take the figures its
        rollout file ended with (best effort; the first turn's own report replaces them)."""
        thread = resumed.get("thread") if isinstance(resumed, Mapping) else None
        path = _text(thread.get("path")) if isinstance(thread, Mapping) else ""
        if not path or self._usage is not None:
            return
        facts = await asyncio.to_thread(read_rollout, path)
        if facts is not None and facts.token_usage is not None and self._usage is None and not self._close_requested:
            self._on_token_usage({"tokenUsage": facts.token_usage})

    async def _load_history(self, client: Any) -> None:
        try:
            saved = await self._saved_turns(client)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # history is best effort; the thread itself matters
            self.transcript.add_notice(f"历史记录加载失败：{friendly_failure(error)}")
        else:
            if saved:
                self.transcript.add_history_turns(build_turns(self.transcript, saved))
        self._changed()

    async def _saved_turns(self, client: Any) -> list[Mapping[str, Any]]:
        return await saved_turns(client, self.session_id)

    def _attach(self, client: Any, result: Any, *, adopt: bool) -> None:
        """Take over the thread an open or resume returned. ``adopt``: its settings become this controller's."""
        result = result if isinstance(result, Mapping) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), Mapping) else {}
        self._client = client
        self.session_id = _text(thread.get("id")) or self.session_id
        self.manager._attach(self)
        model = _text(result.get("model")) or _text(thread.get("model"))
        if model:
            self._model = model
        if adopt:
            effort = _text(result.get("reasoningEffort")) or _text(thread.get("reasoningEffort"))
            if effort:
                self.reasoning = effort
            level = level_of(result.get("sandbox"))
            if level:
                self.autonomy = level
            if model:
                self.requested_model = model
        else:
            self._note_sandbox(result.get("sandbox"))
        if self._model:
            self.transcript.model = self._model
        name = _text(thread.get("name")).strip()
        if name and not self.transcript.title:
            self.transcript.title = name
        if not self.saved_title:
            preview = split_handoff(_text(thread.get("preview")))[0].strip()
            if preview:
                self.saved_title = first_line(preview, 60)
        self.refresh_context()

    def _note_sandbox(self, reported: Any) -> None:
        """Say so when Codex runs the workspace level read-only (no Windows sandbox set up on this machine)."""
        if permission(self.autonomy).sandbox_type == "workspaceWrite" and sandbox_type(reported) == "readOnly":
            self.manager.workspace_write_downgraded = True
            if not self._downgrade_noted:
                self._downgrade_noted = True
                self._notes.append(WORKSPACE_DOWNGRADED)

    async def _forget_thread(self, client: Any, thread_id: str) -> None:
        with contextlib.suppress(CodexError):
            await client.request("thread/unsubscribe", {"threadId": thread_id}, timeout=UNSUBSCRIBE_SECONDS)

    def _give_up(self, message: str) -> None:
        self.state = SessionState.DEAD
        self.last_error = message
        self.transcript.add_error(message, recoverable=True)
        self._notify("state")
        self._changed()

    def _abandon(self, error: BaseException) -> None:
        """Opening raised before a thread existed (e.g. no Codex CLI): there is nothing to close."""
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
        client, thread_id = self._client, self.session_id
        turn_id = self._turn_id if self._turn is not None else None
        self._expire_pending()
        self._release_agents()
        if self.transcript.active_turn is not None:
            self.transcript.abort_turn("本轮已取消。")
        if self._turn is not None:
            self._end_turn(self._turn, quiet=True)
        if client is not None and client.running and thread_id:
            if turn_id:
                with contextlib.suppress(CodexError):
                    await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=2.0)
            await self._forget_thread(client, thread_id)
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
        """The app-server process is gone: DEAD, with a way back (``reconnect``)."""
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
        self._release_agents()
        if turn is not None:
            self._end_turn(turn, quiet=True)
        self._notify("state")
        self._changed()

    async def reconnect(self) -> None:
        """Resume the same thread in a new app-server process after the old one died."""
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
            result = await client.request("thread/resume", self._resume_params(), timeout=THREAD_OPEN_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._close_requested:
                self._give_up(f"重新连接失败：{friendly_failure(error)}")
            return
        if self._close_requested:
            return
        self._needs_resume = False
        self._attach(client, result, adopt=False)
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
        """Record new settings for the next turn (``mode`` is ignored: Codex sessions are always "auto")."""
        if not self.session_id or self.state not in (SessionState.READY, SessionState.RUNNING):
            return False
        if autonomy is not None:
            level = normalize_level(_enum_value(autonomy))
            if level is None:
                self.transcript.add_notice(f"设置更新失败：Codex 没有「{_enum_value(autonomy)}」这个权限档位。")
                self._changed()
                return False
            self.autonomy = level
            if level == "workspace" and self.manager.workspace_write_downgraded and not self._downgrade_noted:
                self._downgrade_noted = True
                self.transcript.add_notice(WORKSPACE_DOWNGRADED)
        if model:
            self.requested_model = self._model = str(model)
            self.transcript.model = self._model
        if reasoning is not None:
            self.reasoning = _enum_value(reasoning)
        self._changed()
        self._notify("settings")
        self.refresh_context()
        return True

    # ------------------------------------------------------------------ turns

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str | None:
        """Why these attachments cannot go to Codex (``None``: nothing known against them).

        Pictures go as data URLs and text files as text items after the message; a PDF only by its extracted text.
        """
        if any(item.kind == IMAGE for item in attachments):
            info = self.manager.model_info(self.model)
            if info is not None and not info.supports_images:
                return f"当前模型「{info.display_name}」不支持图片输入：请先移除图片，或换一个支持图片的模型。"
        for item in attachments:
            if item.kind == PDF and not (item.parsed_text or "").strip():
                return f"Codex 只能读 PDF 里的文字，「{item.name}」里没有提取到文字：请换成图片或文本文件再发送。"
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
        self._turn, self._turn_id = turn, None
        self._turn_done = asyncio.Event()
        self._interrupt_wanted = False
        self._turn_error = None
        self._retry_note = ""
        for note in self._notes:
            self.transcript.add_notice(note)
        self._notes.clear()
        self.transcript.set_working_state("thinking")
        self._spawn(self._start_turn(turn, sent, outgoing))
        self._notify("state")
        self._changed()
        return True

    def start_compaction(self) -> str | None:
        """``/compact``: Codex's own compaction (``thread/compact/start``: the early part of the thread becomes a summary). Codex
        reports it as a turn with a ``contextCompaction`` item, which the page shows like any turn. Why it cannot start (a
        sentence for the user), or ``None`` when it has started."""
        if self.state is not SessionState.READY or not self.session_id or self._client is None:
            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"
        self.used_at = self.active_at = time.time()
        self.state = SessionState.RUNNING
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn("/compact")
        self._turn, self._turn_id = turn, None
        self._turn_done = asyncio.Event()
        self._interrupt_wanted = False
        self._turn_error = None
        self._retry_note = ""
        self.transcript.set_working_state("compacting_conversation")
        self._spawn(self._start_compaction(turn))
        self._notify("state")
        self._changed()
        return None

    async def _start_compaction(self, turn: Turn) -> None:
        client = self._client
        try:
            if client is None or not client.running:
                raise CodexProcessError("codex app-server is not running")
            if self._needs_resume:
                await client.request("thread/resume", self._resume_params(), timeout=THREAD_OPEN_SECONDS)
                self._needs_resume = False
            await client.request("thread/compact/start", {"threadId": self.session_id}, timeout=TURN_START_SECONDS)
        except asyncio.CancelledError:
            if self._turn is turn:
                self.transcript.abort_turn("压缩已取消。")
                self._end_turn(turn)
            raise
        except CodexProcessError as error:
            if self._turn is turn:
                self._connection_lost(error)
            return
        except CodexError as error:
            if isinstance(error, CodexRpcError):
                self._needs_resume = True
            if self._turn is turn:
                self.transcript.fail_turn("没有压缩，对话没有改动：" + friendly_failure(error))
                self._end_turn(turn)
            return
        except Exception as error:  # the session must survive anything
            log.exception("starting a Codex compaction failed")
            if self._turn is turn:
                self.transcript.fail_turn(f"出现内部错误：{type(error).__name__}: {error}")
                self._end_turn(turn)
            return
        if self._turn is turn and self._interrupt_wanted:
            await self._send_interrupt()

    def _turn_params(self, sent: str, attachments: Sequence[Attachment]) -> dict[str, Any]:
        rule = permission(self.autonomy)
        params: dict[str, Any] = {
            "threadId": self.session_id,
            "input": turn_input(sent, attachments),
            "cwd": str(self.project),
            "approvalPolicy": rule.approval,
            "sandboxPolicy": sandbox_policy(self.autonomy, self.project),
        }
        if self.model:
            params["model"] = self.model
        if self.reasoning:
            params["effort"] = self.reasoning
        return params

    async def _start_turn(self, turn: Turn, sent: str, attachments: Sequence[Attachment]) -> None:
        client = self._client
        try:
            if client is None or not client.running:
                raise CodexProcessError("codex app-server is not running")
            params = self._turn_params(sent, attachments)
            if self._needs_resume:
                await client.request("thread/resume", self._resume_params(), timeout=THREAD_OPEN_SECONDS)
                self._needs_resume = False
            result = await client.request("turn/start", params, timeout=TURN_START_SECONDS)
        except asyncio.CancelledError:
            if self._turn is turn:
                self.transcript.abort_turn("本轮已取消。")
                self._end_turn(turn)
            raise
        except CodexProcessError as error:
            if self._turn is turn:
                self._connection_lost(error)
            return
        except (CodexError, ValueError) as error:
            if isinstance(error, CodexRpcError):
                self._needs_resume = True  # e.g. the server unloaded the thread: the retry rejoins it first
            if self._turn is turn:
                message = f"附件无法发送：{error}。" if isinstance(error, ValueError) else friendly_failure(error)
                self.transcript.fail_turn(message)
                self._end_turn(turn)
            return
        except Exception as error:  # the session must survive anything
            log.exception("starting a Codex turn failed")
            if self._turn is turn:
                self.transcript.fail_turn(f"出现内部错误：{type(error).__name__}: {error}")
                self._end_turn(turn)
            return
        started = result.get("turn") if isinstance(result, Mapping) else None
        turn_id = _text(started.get("id")) if isinstance(started, Mapping) else ""
        if self._turn is turn and self._turn_id is None and turn_id:
            self._turn_id = turn_id
        if self._turn is turn and self._interrupt_wanted:
            await self._send_interrupt()

    def _end_turn(self, turn: Turn, *, quiet: bool = False) -> None:
        """The running turn is over (finished, failed, stopped or abandoned): back to READY."""
        if self._turn is not turn:
            return
        self._settle_expired()
        if self._handoff_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.handoff = None  # the thread has the earlier text now; a failed turn keeps it for the retry
            self._handoff_turn = None
        if self._preface_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.preface = ""  # (likewise: it is in the thread now)
            self._preface_turn = None
        if self._recall_turn is turn:
            if turn.status is TurnStatus.DONE:
                self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)
            self._recall_turn, self._recall_ids = None, frozenset()
        if self._turn_id:
            self._ended_turns.add(self._turn_id)
        self._turn, self._turn_id = None, None
        self._interrupt_wanted = False
        self._turn_error = None
        self._retry_note = ""
        self._outputs.clear()
        self._summary_index.clear()
        self._started_ms.clear()
        if self._turn_done is not None:
            self._turn_done.set()
        self.active_at = time.time()
        if self.state is SessionState.RUNNING:
            self.state = SessionState.READY
        if not quiet:
            self._notify("state")
            self._changed()

    async def steer(self, text: str, attachments: Sequence[Attachment] = ()) -> SteerOutcome:
        """Say something to the turn that is running, now (``turn/steer``): Codex adds it to that turn's input, and the
        model reads it at its next step. Codex echoes it at once, so there is no waiting to show."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        turn, client, thread_id = self._turn, self._client, self.session_id
        if self.state is not SessionState.RUNNING or turn is None or client is None or not thread_id:
            return SteerOutcome(False, over=True)
        if not text:
            return SteerOutcome(False, message="没有内容")
        if turn.stopping:
            return SteerOutcome(False, message="本轮正在停止")
        try:
            input_items = turn_input(text, attachments)
        except ValueError as error:
            return SteerOutcome(False, message=f"附件无法发送：{error}")
        loop = asyncio.get_running_loop()
        end = loop.time() + STEER_WAIT_SECONDS
        while self._turn_id is None and self._turn is turn and loop.time() < end:  # turn/start has not answered yet
            await asyncio.sleep(0.05)
        if self._turn is not turn or turn.status is not TurnStatus.RUNNING:
            return SteerOutcome(False, over=True)
        turn_id = self._turn_id
        if turn_id is None:
            return SteerOutcome(False, message="Codex 还没有开始这一轮")
        block = self.transcript.add_steer(turn, text, attachments, pending=False)
        self._changed()
        try:
            await client.request(
                "turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id, "input": input_items}, timeout=STEER_SECONDS
            )
        except asyncio.CancelledError:
            self.transcript.remove_steer(turn, block)
            raise
        except CodexError as error:
            self.transcript.remove_steer(turn, block)
            self._changed()
            if isinstance(error, CodexRpcError) and "no active turn" in error.message.lower():
                return SteerOutcome(False, over=True)  # (measured, 0.166.0: -32600 "no active turn to steer")
            if isinstance(error, CodexProcessError):
                self._connection_lost(error)
            return SteerOutcome(False, message=friendly_failure(error))
        return SteerOutcome(True)

    def take_unadopted(self) -> list[tuple[str, tuple[Attachment, ...]]]:
        """Codex takes a steer in the moment it answers, so none is ever left over (Droid's can be)."""
        return []

    async def interrupt(self) -> None:
        turn = self._turn
        if self.state is not SessionState.RUNNING or turn is None:
            return
        self.transcript.mark_stopping()
        self._changed()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INTERRUPT_GRACE_SECONDS
        if self._turn_id is None:
            self._interrupt_wanted = True  # sent as soon as turn/start has answered
        else:
            await self._send_interrupt(timeout=INTERRUPT_GRACE_SECONDS)
        done = self._turn_done
        if done is not None and self._turn is turn:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), max(0.0, deadline - loop.time()))
        if self._turn is turn:
            log.warning("Codex did not end turn %s within %.0f s of the stop request", self._turn_id, INTERRUPT_GRACE_SECONDS)
            self.transcript.abort_turn("停止超时：Codex 没有及时结束这一轮，这里先停止显示。")
            self._end_turn(turn)

    async def _send_interrupt(self, timeout: float = INTERRUPT_GRACE_SECONDS) -> None:
        client, thread_id, turn_id = self._client, self.session_id, self._turn_id
        if client is None or not thread_id or not turn_id:
            return
        try:
            await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout)
        except CodexTimeoutError:
            pass  # no answer: 0.165.0 does not answer for a turn that has ended; turn/completed decides
        except CodexProcessError as error:
            self._connection_lost(error)
        except CodexRpcError as error:
            if self._turn is not None:
                self.transcript.clear_stopping()
                self.transcript.add_notice(f"停止失败：{error.message}")
                self._changed()

    # ------------------------------------------------------------------ context

    def refresh_context(self) -> None:
        """Codex pushes its figures (``thread/tokenUsage/updated``); this only keeps the window in step with the model."""
        model = self.model
        known = self.manager.model_window(model)
        window = known or (self.context.window if self.context.model == model else None)
        if window != self.context.window or model != self.context.model:
            self._publish_context(replace(self.context, window=window, model=model))

    def _publish_context(self, state: ContextState) -> None:
        if state != self.context:
            self.context = state
            self._notify("context")

    # ------------------------------------------------------------------ notifications

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        handler = _HANDLERS.get(method)
        if handler is None:
            return
        try:
            handler(self, params)
        except Exception:
            log.exception("handling %s for thread %s failed", method, self.session_id)

    def _live_turn(self, params: Mapping[str, Any]) -> Turn | None:
        """The running turn, if ``params`` belong to it (the first turn id seen is taken while turn/start has not answered)."""
        turn = self._turn
        if turn is None or turn.status is not TurnStatus.RUNNING:
            return None
        turn_id = _text(params.get("turnId"))
        if not turn_id:
            return turn
        if self._turn_id is None:
            if turn_id in self._ended_turns:
                return None
            self._turn_id = turn_id
            return turn
        return turn if turn_id == self._turn_id else None

    def _tool(self, turn: Turn, item: Mapping[str, Any], spec: tuple[str, dict[str, Any]]) -> ToolItem:
        name, data = spec
        tool = self.transcript.ensure_tool(turn, _text(item.get("id")), name)
        self.transcript.set_tool_input(turn, tool, data, final=True)
        if item.get("type") == "fileChange":
            self.transcript.set_tool_diffs(turn, tool, file_diffs(file_changes(item)))
        return tool

    def _on_turn_started(self, params: Mapping[str, Any]) -> None:
        raw = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        if self._live_turn({"turnId": _text(raw.get("id"))}) is not None:
            self.transcript.set_working_state("thinking")
            self._changed()

    def _on_turn_completed(self, params: Mapping[str, Any]) -> None:
        raw = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        turn = self._live_turn({"turnId": _text(raw.get("id"))})
        if turn is None:
            return
        milliseconds = _number(raw.get("durationMs"))
        duration = milliseconds / 1000.0 if milliseconds is not None else None
        status = raw.get("status")
        if status == "failed":
            self.transcript.append_error(turn, turn_error_text(raw.get("error") or self._turn_error))
            self.transcript.finish_turn(turn, TurnStatus.FAILED, duration=duration)
        elif status == "interrupted":
            self.transcript.finish_turn(turn, TurnStatus.INTERRUPTED, duration=duration)
        else:
            self.transcript.finish_turn(turn, TurnStatus.DONE, duration=duration)
        self._end_turn(turn)
        self.refresh_context()

    def _on_item_started(self, params: Mapping[str, Any]) -> None:
        turn = self._live_turn(params)
        item = params.get("item")
        if isinstance(item, Mapping) and item.get("type") == "subAgentActivity":
            self._on_agent_activity(item)
            return
        if turn is None or not isinstance(item, Mapping) or not _text(item.get("id")):
            return
        item_id, kind = _text(item.get("id")), item.get("type")
        started = _number(params.get("startedAtMs"))
        if started is not None:
            self._started_ms[item_id] = started
        self._retry_note = ""
        if kind == "reasoning":
            self.transcript.set_working_state("thinking")
        elif kind in TEXT_ITEMS:
            self.transcript.set_working_state("streaming_assistant_message")
        elif kind == "contextCompaction":
            self.transcript.set_working_state("compacting_conversation")
        else:
            spec = tool_spec(item)
            if spec is None:
                return
            tool = self._tool(turn, item, spec)
            self.transcript.tool_running(turn, tool)
            self.transcript.set_working_state("executing_tool")
        self._changed()

    def _on_item_completed(self, params: Mapping[str, Any]) -> None:
        turn = self._live_turn(params)
        item = params.get("item")
        if isinstance(item, Mapping) and item.get("type") == "subAgentActivity":
            self._on_agent_activity(item)
            return
        if turn is None or not isinstance(item, Mapping) or not _text(item.get("id")):
            return
        item_id, kind = _text(item.get("id")), item.get("type")
        started, finished = self._started_ms.pop(item_id, None), _number(params.get("completedAtMs"))
        duration = (finished - started) / 1000.0 if started is not None and finished is not None and finished >= started else None
        if kind in TEXT_ITEMS:
            key = "review" if kind in ("enteredReviewMode", "exitedReviewMode") else "text"
            self.transcript.end_text(turn, item_id, 0, _text(item.get(key)))
        elif kind == "reasoning":
            self.transcript.end_thinking(turn, item_id, 0, text=reasoning_text(item) or None, duration=duration)
            self._summary_index.pop(item_id, None)
        elif kind == "contextCompaction":
            self.transcript.add_notice("Codex 压缩了上下文：较早的对话换成了摘要。")
            self._publish_context(replace(self.context, used=None, compacted=True))
        else:
            spec = tool_spec(item)
            if spec is None:
                return
            tool = self._tool(turn, item, spec)
            text, status = tool_outcome(item, self._outputs.pop(item_id, ""))
            self.transcript.finish_tool(turn, tool, text, status=status)
        self.transcript.set_working_state("thinking")
        self._changed()

    def _delta(self, params: Mapping[str, Any]) -> tuple[Turn, str, str] | None:
        turn = self._live_turn(params)
        item_id, delta = _text(params.get("itemId")), _text(params.get("delta"))
        if turn is None or not item_id or not delta:
            return None
        self._retry_note = ""
        return turn, item_id, delta

    def _on_text_delta(self, params: Mapping[str, Any]) -> None:
        found = self._delta(params)
        if found is None:
            return
        turn, item_id, delta = found
        self.transcript.append_text(turn, item_id, 0, delta)
        self.transcript.set_working_state("streaming_assistant_message")
        self._changed()

    def _on_summary_delta(self, params: Mapping[str, Any]) -> None:
        found = self._delta(params)
        if found is None:
            return
        turn, item_id, delta = found
        index = params.get("summaryIndex")
        index = int(index) if isinstance(index, int) and not isinstance(index, bool) else 0
        previous = self._summary_index.get(item_id)
        entry = turn.thinking.get((item_id, 0))
        separator = "\n\n" if previous is not None and index != previous and entry is not None and entry.text.strip() else ""
        self._summary_index[item_id] = index
        self.transcript.append_thinking(turn, item_id, 0, separator + delta)
        self.transcript.set_working_state("thinking")
        self._changed()

    def _on_reasoning_delta(self, params: Mapping[str, Any]) -> None:
        found = self._delta(params)
        if found is None:
            return
        turn, item_id, delta = found
        if item_id in self._summary_index:
            return  # the summary is what is shown; raw reasoning only when there is none
        self.transcript.append_thinking(turn, item_id, 0, delta)
        self._changed()

    def _on_output_delta(self, params: Mapping[str, Any]) -> None:
        found = self._delta(params)
        if found is None:
            return
        turn, item_id, delta = found
        output = (self._outputs.get(item_id, "") + delta)[-OUTPUT_KEEP_CHARS:]
        self._outputs[item_id] = output
        tool = turn.tools.get(item_id) or self.transcript.ensure_tool(turn, item_id, EXECUTE)
        self.transcript.tool_running(turn, tool, output)
        self._changed()

    def _on_mcp_progress(self, params: Mapping[str, Any]) -> None:
        turn = self._live_turn(params)
        tool = turn.tools.get(_text(params.get("itemId"))) if turn is not None else None
        if turn is not None and tool is not None and _text(params.get("message")):
            self.transcript.tool_running(turn, tool, _text(params.get("message")))
            self._changed()

    def _on_patch_updated(self, params: Mapping[str, Any]) -> None:
        turn = self._live_turn(params)
        tool = turn.tools.get(_text(params.get("itemId"))) if turn is not None else None
        if turn is not None and tool is not None:
            changes = [change for change in params.get("changes") or () if isinstance(change, Mapping)]
            self.transcript.set_tool_diffs(turn, tool, file_diffs(change for change in changes if isinstance(change.get("path"), str)))
            self._changed()

    def _on_plan_updated(self, params: Mapping[str, Any]) -> None:
        turn = self._live_turn(params)
        todos = plan_todos(params.get("plan"))
        if turn is None or not todos:
            return
        self._plans += 1
        tool = self.transcript.ensure_tool(turn, f"plan-{self._plans}", TODO)
        data: dict[str, Any] = {"todos": todos}
        if _text(params.get("explanation")).strip():
            data["explanation"] = _text(params.get("explanation")).strip()
        self.transcript.set_tool_input(turn, tool, data, final=True)
        self.transcript.finish_tool(turn, tool, "计划已更新。", status=ToolStatus.DONE)
        self._changed()

    def _on_token_usage(self, params: Mapping[str, Any]) -> None:
        usage = params.get("tokenUsage")
        if not isinstance(usage, Mapping):
            return
        total = usage.get("total")
        if isinstance(total, Mapping):
            self._usage = CodexUsage.from_breakdown(total)
        window = _number(usage.get("modelContextWindow"))
        window_tokens = int(window) if window and window > 0 else None
        if window_tokens:
            self.manager._learn_window(self.model, window_tokens)
        used = last_call_tokens(usage.get("last"))
        context = self.context
        self._publish_context(
            replace(
                context,
                used=used if used is not None else context.used,
                window=window_tokens or context.window,
                model=self.model,
                compacted=context.compacted and used is None,
            )
        )
        self._changed()

    def _on_error(self, params: Mapping[str, Any]) -> None:
        error = params.get("error") if isinstance(params.get("error"), Mapping) else {}
        if params.get("willRetry"):
            self._retry_note = retry_text(error.get("message"))
            self._changed()
            return
        if self._live_turn(params) is not None:
            self._turn_error = error  # turn/completed (failed) follows and shows it, once
            return
        self.transcript.add_error(turn_error_text(error))
        self._changed()

    def _on_warning(self, params: Mapping[str, Any]) -> None:
        message = _text(params.get("message")).strip()
        if not message or message.startswith(TRANSPORT_NOISE) or message in self._warnings:
            return
        self._warnings.add(message)
        self.transcript.add_notice(f"Codex 提示：{message}")
        self._changed()

    def _on_rerouted(self, params: Mapping[str, Any]) -> None:
        if self._live_turn(params) is None:
            return
        target, source = _text(params.get("toModel")), _text(params.get("fromModel"))
        if target:
            self.transcript.add_notice(f"Codex 把这一轮交给了 {target}（请求的是 {source or self.model}）。")
            self._changed()

    def _on_name(self, params: Mapping[str, Any]) -> None:
        name = _text(params.get("threadName")).strip()
        if name and name != self.transcript.title:
            self.transcript.title = name
            self._changed()

    def _on_thread_closed(self, params: Mapping[str, Any]) -> None:
        """The server unloaded the thread: the next turn resumes it first."""
        if self._close_requested:
            return
        self._needs_resume = True
        turn = self._turn
        if turn is not None:
            self.transcript.fail_turn("Codex 关闭了这个会话的线程，本轮没有完成。可以重新发送。")
            self._end_turn(turn)

    def _on_request_resolved(self, params: Mapping[str, Any]) -> None:
        prompt_id = self._requests.get(params.get("requestId"))
        if prompt_id is not None:
            self._expire_prompt(prompt_id)
            self._changed()

    def _on_compacted(self, params: Mapping[str, Any]) -> None:
        self._publish_context(replace(self.context, used=None, compacted=True))

    # ------------------------------------------------------------------ server requests (approvals, questions)

    async def _on_server_request(self, method: str, params: Mapping[str, Any], request_id: Any) -> Any:
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return await self._approval(method, params, request_id)
        if method == "item/permissions/requestApproval":
            return await self._permissions(params, request_id)
        if method == "item/tool/requestUserInput":
            return await self._questions(params, request_id)
        if method == "mcpServer/elicitation/request":
            return await self._elicitation(params, request_id)
        if method == "item/tool/call":
            return {"contentItems": [], "success": False}
        if method in ("execCommandApproval", "applyPatchApproval"):
            return {"decision": "abort"}  # the old (v1) protocol; v2 threads use the requests above
        raise CodexRpcError(METHOD_NOT_FOUND, f"WittyCode does not handle {method}")

    async def _approval(self, method: str, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        turn = self._live_turn(params)
        item_id = _text(params.get("itemId"))
        if turn is None or not item_id:
            return {"decision": EXPIRED_DECISION}
        if method == "item/commandExecution/requestApproval":
            tool = self.transcript.ensure_tool(turn, item_id, EXECUTE)
            if not tool.input_final and _text(params.get("command")):
                data = {"command": _text(params.get("command"))}
                if _text(params.get("cwd")):
                    data["cwd"] = _text(params.get("cwd"))
                self.transcript.set_tool_input(turn, tool, data, final=True)
            action = command_action(item_id, params, tool)
        else:
            tool = self.transcript.ensure_tool(turn, item_id, "Edit")
            action = file_action(item_id, params, tool)
        prompt = self.transcript.add_permission_prompt([item_id], [action], APPROVAL_OPTIONS)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return {"decision": EXPIRED_DECISION}
        value = await self._wait(prompt.id, request_id)
        return {"decision": decision_for(value)}

    async def _permissions(self, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        turn = self._live_turn(params)
        item_id = _text(params.get("itemId")) or f"permissions-{request_id}"
        refused: dict[str, Any] = {"permissions": {}, "scope": "turn"}
        if turn is None:
            return refused
        tool = self.transcript.ensure_tool(turn, item_id, "RequestPermissions")
        self.transcript.set_tool_input(turn, tool, {"reason": _text(params.get("reason"))}, final=True)
        prompt = self.transcript.add_permission_prompt([item_id], [permissions_action(item_id, params)], APPROVAL_OPTIONS)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return refused
        value = await self._wait(prompt.id, request_id)
        wanted = params.get("permissions") if isinstance(params.get("permissions"), Mapping) else {}
        if value in ("proceed_once", "proceed_always"):
            granted = {key: grant for key, grant in wanted.items() if grant is not None}
            self.transcript.finish_tool(turn, tool, "已授予。", status=ToolStatus.DONE)
            return {"permissions": granted, "scope": "session" if value == "proceed_always" else "turn"}
        self.transcript.finish_tool(turn, tool, "已拒绝。", status=ToolStatus.CANCELLED)
        return refused

    async def _questions(self, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        turn = self._live_turn(params)
        item_id = _text(params.get("itemId")) or f"question-{request_id}"
        questions = [question for question in params.get("questions") or () if isinstance(question, Mapping)]
        if turn is None or not questions:
            return {"answers": {}}
        views = tuple(
            QuestionView(
                index=index,
                topic=_text(question.get("header")),
                question=_text(question.get("question")),
                options=tuple(
                    _text(option.get("label")) for option in question.get("options") or () if isinstance(option, Mapping) and _text(option.get("label"))
                ),
                multi_select=False,
            )
            for index, question in enumerate(questions)
        )
        prompt = self.transcript.add_question_prompt(item_id, views)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return {"answers": {}}
        value = await self._wait(prompt.id, request_id)
        tool = turn.tools.get(item_id)
        if value is None:
            if tool is not None:
                self.transcript.finish_tool(turn, tool, "已跳过。", status=ToolStatus.CANCELLED)
            return {"answers": {}}
        answers = {
            _text(question.get("id")) or str(index): {"answers": list(answer)} for index, (question, answer) in enumerate(zip(questions, value))
        }
        if tool is not None:
            self.transcript.finish_tool(turn, tool, "\n".join(", ".join(answer) for answer in value), status=ToolStatus.DONE)
        return {"answers": answers}

    async def _elicitation(self, params: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
        """An MCP server asks the user for something (see ``elicit``): shown as questions, answered as ``accept`` /
        ``decline`` / ``cancel``. Anything that cannot be shown is declined, and the user is told."""
        declined = {"action": "decline", "content": None, "_meta": None}
        server = _text(params.get("serverName")) or "MCP"
        plan = plan_of(params)
        if plan is None:
            self.transcript.add_notice(f"MCP 服务器「{server}」发来一种 WittyCode 不会显示的请求（{_text(params.get('mode')) or '未知'}），已拒绝。")
            self._changed()
            return declined
        turn = self._live_turn(params)
        if turn is None:
            self.transcript.add_notice(f"MCP 服务器「{server}」在回合之外请求补充信息，已拒绝。")
            self._changed()
            return declined
        item_id = f"elicitation-{request_id}"
        prompt = self.transcript.add_question_prompt(item_id, plan.views)
        self._changed()
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return {"action": "cancel", "content": None, "_meta": None}
        value = await self._wait(prompt.id, request_id)
        reply = reply_for(plan, value)
        if plan.mode == "url" and reply["action"] == "accept":
            self.manager.open_external(plan.url)
        tool = turn.tools.get(item_id)
        if tool is not None:
            done = {"accept": "已回答。", "decline": "已拒绝。", "cancel": "已跳过。"}[reply["action"]]
            self.transcript.finish_tool(turn, tool, done, status=ToolStatus.DONE if reply["action"] == "accept" else ToolStatus.CANCELLED)
        return reply

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
        """Answer (or, with ``None``, skip) a pending question; each answer is a string or a list of strings."""
        future = self._pending.get(prompt_id)
        if future is None:
            return False
        texts = None if answers is None else [_answer_text(answer) for answer in answers]
        if not self.transcript.answer_question(prompt_id, texts):
            return False
        if not future.done():
            future.set_result(None if answers is None else [[answer] if isinstance(answer, str) else list(answer) for answer in answers])
        return True

    def _expire_prompt(self, prompt_id: str) -> None:
        self.transcript.expire_prompt(prompt_id)
        future = self._pending.pop(prompt_id, None)
        if future is not None and not future.done():
            future.set_result(None)  # answered "cancel": the request handler returns to Codex normally

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


NO_TASK_TEXT = "主智能体交给它的任务（Codex 没有提供原文）"


@dataclass(eq=False)
class CodexAgent(CodexController):
    """A sub-agent: another Codex thread that Codex runs for its parent (``spawn_agent``).

    Nobody opens it here: the app-server sends its notifications and its approvals on the parent's connection under its
    own thread id, so it only needs a transcript to put them in. It is not a conversation (it has no tab, no composer and
    nothing the engine lists), so what it has to say goes to the parent (``_agent_changed``), which shows it in the
    page's side panel. Its approvals are asked of the user like the parent's, with the parent's permission level: that
    is how Codex starts it (the same approval policy and sandbox as the thread that spawned it, checked in its rollout).
    """

    parent: CodexController | None = None
    path: str = ""
    """``/root/coder_create_sub``: who started it and what it is called."""
    agent_model: str = ""
    agent_effort: str = ""
    nickname: str = ""
    role: str = ""
    task: str = ""
    """What it was asked to do (the first line of its first message)."""
    started_at_s: float = field(default_factory=time.time)
    finished_at_s: float | None = None

    @property
    def name(self) -> str:
        tail = self.path.rstrip("/").rsplit("/", 1)[-1] if self.path else ""
        return self.nickname or tail or (self.session_id or "")[-6:]

    @property
    def status(self) -> str:
        """``working`` | ``waiting`` (for the user's answer) | ``done`` | ``stopped`` | ``failed``."""
        if self.state is SessionState.RUNNING:
            return "waiting" if self.transcript.pending_prompts() else "working"
        last = self.transcript.turns[-1] if self.transcript.turns else None
        if last is None:
            return "working"  # started, its first turn not seen yet
        return {TurnStatus.DONE: "done", TurnStatus.INTERRUPTED: "stopped", TurnStatus.FAILED: "failed"}.get(last.status, "working")

    def _notify(self, kind: str) -> None:
        if self.parent is not None:
            self.parent._agent_changed(self)

    def refresh_context(self) -> None:
        return

    async def learn(self) -> None:
        """Who it is and what it was asked, from the thread itself (the notifications do not say)."""
        client = self._client
        if client is None or not self.session_id:
            return
        with contextlib.suppress(CodexError, asyncio.TimeoutError):
            result = await client.request("thread/read", {"threadId": self.session_id, "includeTurns": False}, timeout=10.0)
            thread = result.get("thread") if isinstance(result, Mapping) else None
            if isinstance(thread, Mapping):
                self.nickname = _text(thread.get("agentNickname"))
                self.role = _text(thread.get("agentRole"))
                self.task = _text(thread.get("preview")).strip().splitlines()[0][:200] if _text(thread.get("preview")).strip() else ""
                first = self.transcript.turns[0].user_block if self.transcript.turns else None
                if first is not None and self.task:
                    first.text = self.task
                    self.transcript.turns[0].version += 1
                self._changed()

    def _observe_turn(self, turn_id: str) -> None:
        """A turn of this thread is under way (we never started it): give it a turn of its own in the transcript."""
        running = self._turn is not None and self._turn.status is TurnStatus.RUNNING
        if running or not turn_id or turn_id in self._ended_turns:
            return
        # (Codex does not hand out what the thread was asked: a sub-agent thread's preview is empty and its turns cannot be
        # read, checked on 0.166.0, so the first block says so unless the thread does give it)
        turn = self.transcript.start_turn(self.task or NO_TASK_TEXT)
        if turn.user_block is not None:
            turn.user_block.who = "主智能体"
        self._turn, self._turn_id = turn, turn_id
        self._turn_done = asyncio.Event()
        self.state = SessionState.RUNNING
        self.transcript.set_working_state("thinking")

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method == "thread/tokenUsage/updated":
            # Only its count: the parent's context bar is not the sub-agent's, and its tokens are part of the conversation's.
            usage = params.get("tokenUsage")
            total = usage.get("total") if isinstance(usage, Mapping) else None
            if isinstance(total, Mapping):
                self._usage = CodexUsage.from_breakdown(total)
                self._changed()
            return
        if method in ("thread/closed", "thread/name/updated", "warning"):
            # The server unloading an idle sub-agent is nothing the user needs to hear about; its start-up warnings (a
            # tool server that would not start) are the thread's own, already shown there, and one that arrives before
            # the agent's first turn would open a turn of its own that the real one then cuts short.
            return
        if method == "turn/started":
            raw = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
            self._observe_turn(_text(raw.get("id")))
        elif method.startswith("item/") and (self._turn is None or self._turn.status is not TurnStatus.RUNNING):
            self._observe_turn(_text(params.get("turnId")))  # joined in the middle of a turn
        super()._on_notification(method, params)
        if method == "turn/completed":
            self.finished_at_s = time.time()


_HANDLERS: dict[str, Callable[[CodexController, Mapping[str, Any]], None]] = {
    "turn/started": CodexController._on_turn_started,
    "turn/completed": CodexController._on_turn_completed,
    "item/started": CodexController._on_item_started,
    "item/completed": CodexController._on_item_completed,
    "item/agentMessage/delta": CodexController._on_text_delta,
    "item/plan/delta": CodexController._on_text_delta,
    "item/reasoning/summaryTextDelta": CodexController._on_summary_delta,
    "item/reasoning/textDelta": CodexController._on_reasoning_delta,
    "item/commandExecution/outputDelta": CodexController._on_output_delta,
    "item/mcpToolCall/progress": CodexController._on_mcp_progress,
    "item/fileChange/patchUpdated": CodexController._on_patch_updated,
    "turn/plan/updated": CodexController._on_plan_updated,
    "thread/tokenUsage/updated": CodexController._on_token_usage,
    "error": CodexController._on_error,
    "warning": CodexController._on_warning,
    "model/rerouted": CodexController._on_rerouted,
    "thread/name/updated": CodexController._on_name,
    "thread/closed": CodexController._on_thread_closed,
    "serverRequest/resolved": CodexController._on_request_resolved,
    "thread/compacted": CodexController._on_compacted,
}
