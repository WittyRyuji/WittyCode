"""Transcript view model and the builder API every engine's controller writes it with.

Each engine's controller turns what its engine reports (live, or a saved session read back) into calls of the builder API
here (``start_turn``, ``add_text`` …, ``add_permission_prompt``, ``add_question_prompt``). Pure: no Qt, no I/O.

Layout of a turn: the user message, then assistant text blocks, with everything
the agent did between two texts (thinking + tool calls) folded into one
``ActivityBlock``. Pending permission / AskUser prompts live in ``Turn.prompts``
and are shown below the blocks until answered.

Every node has a ``version`` that is bumped on change, so the renderer can send
incremental patches instead of re-rendering whole turns.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar


from desktop.core.attachments import Attachment
from desktop.core.diffs import FileDiff, input_diffs, result_diff
from desktop.core.prompts import ActionView, OptionView, QuestionView
from desktop.core.toolinfo import Category, TodoEntry, describe_input, plan_steps, tool_category

class WorkingState(str, Enum):
    """What a running turn is doing (the page's labels are in ``toolinfo.working_label``)."""

    IDLE = "idle"
    THINKING = "thinking"
    STREAMING_ASSISTANT_MESSAGE = "streaming_assistant_message"
    WAITING_FOR_TOOL_CONFIRMATION = "waiting_for_tool_confirmation"
    EXECUTING_TOOL = "executing_tool"
    COMPACTING_CONVERSATION = "compacting_conversation"


HIDDEN_MESSAGE_PREFIXES = ("context-", "todo-sug", "__internal__")
PROGRESS_TAIL_CHARS = 4000
_CANCEL_MARKERS = ("tool execution cancelled", "cancelled by user", "batch cancelled", "interrupted by user")
_ERROR_TRANSLATIONS = {
    "Permission interaction handler failed": "审批处理出错，已按「拒绝」处理这一步。",
    "Question interaction handler failed": "提问处理出错，已按「取消」处理这个问题。",
}


class ToolStatus(str, Enum):
    PENDING = "pending"
    AWAITING_PERMISSION = "awaiting_permission"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


TERMINAL_TOOL_STATUSES = frozenset({ToolStatus.DONE, ToolStatus.ERROR, ToolStatus.CANCELLED})


class TurnStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class PromptStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    """Settled by the engine (the call ended) without an answer from us."""
    EXPIRED = "expired"
    """The turn ended, was interrupted, or the session closed while the prompt was open."""


def is_hidden_message(message_id: str | None, visibility: str | None = None) -> bool:
    if visibility == "llm_only":
        return True
    return bool(message_id) and message_id.startswith(HIDDEN_MESSAGE_PREFIXES)


def plain(value: Any) -> Any:
    """Frozen SDK JSON (mappings / tuples) to plain dicts and lists."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def result_text(content: Any) -> str:
    """Flatten a tool result (string, JSON list, or SDK content blocks) to display text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        if isinstance(content.get("text"), str):
            return content["text"]
        if content.get("type") == "image":
            return "[图片]"
        return str(plain(content))
    if isinstance(content, (list, tuple)):
        return "\n".join(part for part in (result_text(item) for item in content) if part)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    if getattr(content, "type", None) is not None and "image" in str(getattr(content, "type")):
        return "[图片]"
    return str(content)


def result_status(text: str, is_error: bool) -> ToolStatus:
    if not is_error:
        return ToolStatus.DONE
    lowered = text.lower()
    if any(marker in lowered for marker in _CANCEL_MARKERS):
        return ToolStatus.CANCELLED
    return ToolStatus.ERROR


def friendly_error(message: str) -> str:
    return _ERROR_TRANSLATIONS.get(message.strip(), message)


# --------------------------------------------------------------------------- nodes


@dataclass(eq=False)
class ToolItem:
    id: str
    tool_use_id: str
    name: str
    category: Category
    input: dict[str, Any] = field(default_factory=dict)
    input_final: bool = False
    status: ToolStatus = ToolStatus.PENDING
    progress: str = ""
    result: str | None = None
    is_error: bool = False
    duplicate_results: int = 0
    diffs: tuple[FileDiff, ...] = ()
    diff_source: str = ""
    decision: str | None = None
    message_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    version: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TOOL_STATUSES

    @property
    def description(self) -> str:
        return describe_input(self.name, self.input)


@dataclass(eq=False)
class ThinkingEntry:
    id: str
    message_id: str | None = None
    block_index: int | None = None
    text: str = ""
    duration: float | None = None
    complete: bool = False
    redacted: bool = False
    version: int = 0


ActivityEntry = ToolItem | ThinkingEntry


@dataclass(eq=False)
class UserBlock:
    id: str
    text: str
    message_id: str | None = None
    version: int = 0
    attachments: tuple[Attachment, ...] = ()
    handoff: tuple[int, bool] | None = None
    """(messages, truncated): this message went out with the earlier conversation's text in front of it (engine change)."""
    who: str = ""
    """Who wrote it when that is not the user (a sub-agent's task comes from the agent that started it): the page's label."""
    steer: bool = False
    """Said while the turn was running, to steer it: not the message that started the turn."""
    pending: bool = False
    """A steer the engine has not taken in yet (an engine does so after the step it is on)."""
    kind: ClassVar[str] = "user"


@dataclass(eq=False)
class TextBlockView:
    id: str
    text: str = ""
    message_id: str | None = None
    block_index: int | None = None
    streaming: bool = False
    version: int = 0
    kind: ClassVar[str] = "text"


@dataclass(frozen=True, slots=True)
class ActivityStats:
    steps: int
    counts: dict[Category, int]
    errors: int
    cancelled: int
    thinking: int
    active: ToolItem | None


def activity_stats(entries: Iterable[ActivityEntry]) -> ActivityStats:
    counts: dict[Category, int] = {}
    steps = errors = cancelled = thinking = 0
    awaiting = running = pending = None
    for entry in entries:
        if isinstance(entry, ThinkingEntry):
            thinking += 1
            continue
        steps += 1
        counts[entry.category] = counts.get(entry.category, 0) + 1
        if entry.status is ToolStatus.ERROR:
            errors += 1
        elif entry.status is ToolStatus.CANCELLED:
            cancelled += 1
        elif entry.status is ToolStatus.AWAITING_PERMISSION:
            awaiting = entry
        elif entry.status is ToolStatus.RUNNING:
            running = entry
        elif entry.status is ToolStatus.PENDING:
            pending = entry
    return ActivityStats(steps, counts, errors, cancelled, thinking, awaiting or running or pending)


@dataclass(eq=False)
class ActivityBlock:
    id: str
    entries: list[ActivityEntry] = field(default_factory=list)
    version: int = 0
    kind: ClassVar[str] = "activity"

    @property
    def tools(self) -> list[ToolItem]:
        return [entry for entry in self.entries if isinstance(entry, ToolItem)]

    def stats(self) -> ActivityStats:
        return activity_stats(self.entries)


@dataclass(eq=False)
class QABlock:
    id: str
    pairs: list[tuple[str, str]]
    cancelled: bool = False
    version: int = 0
    kind: ClassVar[str] = "qa"


@dataclass(eq=False)
class ErrorBlock:
    id: str
    message: str
    recoverable: bool = False
    resolved: bool = False
    version: int = 0
    kind: ClassVar[str] = "error"


@dataclass(eq=False)
class NoticeBlock:
    id: str
    text: str
    version: int = 0
    kind: ClassVar[str] = "notice"


@dataclass(eq=False)
class SwitchBlock:
    """The line where a conversation changed engine (the own loop <-> Devin): what carries over to the new one."""

    id: str
    from_model: str
    from_engine: str
    to_model: str
    to_engine: str
    carried: int
    total: int
    size: int
    truncated: bool
    version: int = 0
    kind: ClassVar[str] = "switch"


Block = UserBlock | TextBlockView | ActivityBlock | QABlock | ErrorBlock | NoticeBlock | SwitchBlock


@dataclass(eq=False)
class PermissionPrompt:
    id: str
    tool_use_ids: tuple[str, ...]
    actions: tuple[ActionView, ...]
    options: tuple[OptionView, ...]
    status: PromptStatus = PromptStatus.PENDING
    selected: str | None = None
    version: int = 0
    kind: ClassVar[str] = "permission"


@dataclass(eq=False)
class QuestionPrompt:
    id: str
    tool_call_id: str
    questions: tuple[QuestionView, ...]
    status: PromptStatus = PromptStatus.PENDING
    answers: tuple[str, ...] | None = None
    version: int = 0
    who: str = ""
    """The actual asker, captured when asked rather than inferred from a later model selection."""
    kind: ClassVar[str] = "question"


Prompt = PermissionPrompt | QuestionPrompt


@dataclass(frozen=True)
class PlanSnapshot:
    id: str
    steps: tuple[TodoEntry, ...]


@dataclass(eq=False)
class Turn:
    id: str
    blocks: list[Block] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    status: TurnStatus = TurnStatus.RUNNING
    started_at: float | None = None
    finished_at: float | None = None
    duration: float | None = None
    usage: Any = None
    model: str | None = None
    from_history: bool = False
    working_state: str | None = None
    stopping: bool = False
    version: int = 0
    first_output_at: float | None = None
    """When the model first produced anything in this turn (text, thinking, a tool call): the wait for it is the latency."""
    blocked_seconds: float = 0.0
    """Time the turn spent waiting for something other than the model: tools at work, answers the user had to give."""
    output_tokens: int | None = None
    """What the model wrote in this turn (thinking included), when the engine said; with ``model_seconds`` it is the speed."""
    model_seconds: float | None = None
    """The turn's time without ``blocked_seconds``."""
    blocked: bool = field(default=False, repr=False)
    ticked_at: float | None = field(default=None, repr=False)
    out_base: int | None = field(default=None, repr=False)
    out_trusted: bool = field(default=False, repr=False)
    tools: dict[str, ToolItem] = field(default_factory=dict)
    texts: dict[tuple[str, int], TextBlockView] = field(default_factory=dict, repr=False)
    thinking: dict[tuple[str, int], ThinkingEntry] = field(default_factory=dict, repr=False)
    group_of: dict[str, ActivityBlock] = field(default_factory=dict, repr=False)
    plan: PlanSnapshot | None = None
    """Latest accepted plan in event order, independent of historical tool row positions; empty steps explicitly clear it."""

    @property
    def running(self) -> bool:
        return self.status is TurnStatus.RUNNING

    @property
    def user_block(self) -> UserBlock | None:
        first = self.blocks[0] if self.blocks else None
        return first if isinstance(first, UserBlock) else None

    def activity_entries(self) -> list[ActivityEntry]:
        return [entry for block in self.blocks if isinstance(block, ActivityBlock) for entry in block.entries]

    def stats(self) -> ActivityStats:
        return activity_stats(self.activity_entries())

    def pending_prompts(self) -> list[Prompt]:
        return [prompt for prompt in self.prompts if prompt.status is PromptStatus.PENDING]


# --------------------------------------------------------------------------- reducer


class Transcript:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.turns: list[Turn] = []
        self.version = 0
        self.title: str | None = None
        self.usage: Any = None
        self.working_state: WorkingState | None = None
        self.model: str | None = None
        self.output_probe: Callable[[], int | None] | None = None
        """What the engine's session has counted of output tokens so far (``None``: not yet): how a turn's output is found
        where the engine reports totals, not per turn."""
        self._clock = clock
        self._counter = itertools.count(1)
        self._attachments: dict[str, Attachment] = {}
        self._items: dict[str, ToolItem] = {}
        self._prompts: dict[str, tuple[Turn, Prompt]] = {}

    # ---------------------------------------------------------------- queries

    @property
    def active_turn(self) -> Turn | None:
        if self.turns and self.turns[-1].status is TurnStatus.RUNNING and not self.turns[-1].from_history:
            return self.turns[-1]
        return None

    def item(self, item_id: str) -> ToolItem | None:
        return self._items.get(item_id)

    def attachment(self, attachment_id: str) -> Attachment | None:
        return self._attachments.get(attachment_id)

    def prompt(self, prompt_id: str) -> Prompt | None:
        found = self._prompts.get(prompt_id)
        return found[1] if found else None

    def pending_prompts(self) -> list[Prompt]:
        return [prompt for _, prompt in self._prompts.values() if prompt.status is PromptStatus.PENDING]

    # ---------------------------------------------------------------- helpers

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._counter)}"

    def _touch(self, turn: Turn | None, *nodes: Any) -> None:
        for node in nodes:
            if node is not None:
                node.version += 1
        if turn is not None:
            turn.version += 1
            self._tick(turn)
        self.version += 1

    def _tick(self, turn: Turn) -> None:
        """Every change to a turn comes through ``_touch`` after it was made, so the time since the last change belongs to what
        the turn was waiting for then: a tool at work or a question to the user is not the model's time."""
        if turn.ticked_at is None or turn.from_history or turn.status is not TurnStatus.RUNNING:
            return
        now = self._clock()
        if turn.blocked:
            turn.blocked_seconds += max(0.0, now - turn.ticked_at)
        turn.ticked_at = now
        turn.blocked = any(item.status in (ToolStatus.RUNNING, ToolStatus.AWAITING_PERMISSION) for item in turn.tools.values()) or any(
            prompt.status is PromptStatus.PENDING for prompt in turn.prompts
        )

    def _note_output(self, turn: Turn) -> None:
        if turn.first_output_at is None and not turn.from_history:
            turn.first_output_at = self._clock()

    def _touch_item(self, turn: Turn, item: ActivityEntry) -> None:
        self._touch(turn, item, turn.group_of.get(item.id))

    def _append_block(self, turn: Turn, block: Block) -> Block:
        turn.blocks.append(block)
        self._touch(turn, block)
        return block

    def _trailing_group(self, turn: Turn) -> ActivityBlock:
        if turn.blocks and isinstance(turn.blocks[-1], ActivityBlock):
            return turn.blocks[-1]
        group = ActivityBlock(id=self.new_id("b"))
        turn.blocks.append(group)
        return group


    def _own(self, attachments: Iterable[Attachment]) -> tuple[Attachment, ...]:
        """The transcript's copies of attachments (slimmed, with ids the page can refer to)."""
        owned = tuple(replace(item.slim(), id=self.new_id("a")) for item in attachments)
        for item in owned:
            self._attachments[item.id] = item
        return owned

    # ---------------------------------------------------------------- builder API (live + history)

    def set_plan(self, turn: Turn, value: Any, *, source_id: str = "") -> bool:
        steps = plan_steps(value)
        if steps is None or (turn.plan is not None and turn.plan.steps == steps):
            return False
        turn.plan = PlanSnapshot(source_id or self.new_id("p"), steps)
        self._touch(turn)
        return True

    def _accept_tool_plan(self, turn: Turn, item: ToolItem) -> None:
        if item.category is Category.PLAN and item.status is ToolStatus.DONE and item.input_final:
            self.set_plan(turn, item.input.get("todos"), source_id=item.id)

    def ensure_tool(self, turn: Turn, tool_use_id: str, name: str, *, message_id: str | None = None) -> ToolItem:
        item = turn.tools.get(tool_use_id)
        if item is None:
            item = ToolItem(
                id=self.new_id("i"),
                tool_use_id=tool_use_id,
                name=name or "?",
                category=tool_category(name or ""),
            )
            group = self._trailing_group(turn)
            group.entries.append(item)
            turn.tools[tool_use_id] = item
            turn.group_of[item.id] = group
            self._items[item.id] = item
            self._note_output(turn)
        elif name and item.name == "?":
            item.name = name
            item.category = tool_category(name)
        if message_id and item.message_id is None:
            item.message_id = message_id
        return item

    def set_tool_input(self, turn: Turn, item: ToolItem, data: Mapping[str, Any], *, final: bool, accept_plan: bool = True) -> None:
        if item.input_final and not final:
            return
        incoming = plain(data)
        changed = item.input != incoming
        item.input = incoming
        if final:
            item.input_final = True
            if item.diff_source not in ("result", "permission"):
                diffs = input_diffs(item.name, item.input)
                if diffs:
                    item.diffs = diffs
                    item.diff_source = "input"
        if changed and final and accept_plan:
            self._accept_tool_plan(turn, item)
        self._touch_item(turn, item)

    def text_block(self, turn: Turn, message_id: str, block_index: int) -> TextBlockView:
        key = (message_id, block_index)
        block = turn.texts.get(key)
        if block is None:
            block = TextBlockView(id=self.new_id("b"), message_id=message_id, block_index=block_index)
            turn.texts[key] = block
            turn.blocks.append(block)
            self._note_output(turn)
        return block

    def thinking_entry(self, turn: Turn, message_id: str, block_index: int) -> ThinkingEntry:
        key = (message_id, block_index)
        entry = turn.thinking.get(key)
        if entry is None:
            entry = ThinkingEntry(id=self.new_id("k"), message_id=message_id, block_index=block_index)
            group = self._trailing_group(turn)
            group.entries.append(entry)
            turn.thinking[key] = entry
            turn.group_of[entry.id] = group
            self._note_output(turn)
        return entry

    def record_result(self, turn: Turn, item: ToolItem, content: Any, is_error: bool | None, *, at: float | None = None) -> None:
        if item.result is not None:
            # Interrupts can deliver a second result for the same tool; the first one wins.
            item.duplicate_results += 1
            self._touch_item(turn, item)
            return
        text = result_text(content)
        item.result = text
        item.is_error = bool(is_error)
        item.status = result_status(text, item.is_error)
        item.finished_at = self._clock() if at is None else at
        if not item.is_error:
            diff = result_diff(item.name, text, item.input)
            if diff is not None:
                item.diffs = (diff,)
                item.diff_source = "result"
        self._settle_prompts_for(turn, item)
        self._accept_tool_plan(turn, item)
        self._touch_item(turn, item)

    def new_history_turn(
        self,
        text: str | None,
        *,
        message_id: str | None = None,
        started_at: float | None = None,
        attachments: Iterable[Attachment] = (),
    ) -> Turn:
        turn = Turn(id=self.new_id("t"), started_at=started_at, from_history=True)
        if text is not None:
            block = UserBlock(id=self.new_id("b"), text=text, message_id=message_id)
            block.attachments = self._own(attachments)
            turn.blocks.append(block)
        return turn

    def finish_turn(
        self,
        turn: Turn,
        status: TurnStatus,
        *,
        duration: float | None = None,
        usage: Any = None,
        finished_at: float | None = None,
    ) -> None:
        self._tick(turn)  # (while it still runs: the last stretch of waiting is counted)
        turn.status = status
        turn.finished_at = finished_at if finished_at is not None else (turn.finished_at if turn.from_history else self._clock())
        if duration is None and turn.started_at is not None and turn.finished_at is not None:
            duration = max(0.0, turn.finished_at - turn.started_at)
        turn.duration = duration
        turn.usage = usage
        if not turn.from_history and duration is not None:
            turn.model_seconds = max(0.0, duration - turn.blocked_seconds)
            turn.output_tokens = self._turn_output(turn, usage)
        turn.stopping = False
        if turn.model is None and not turn.from_history:
            turn.model = self.model
        for item in turn.tools.values():
            if not item.terminal:
                item.status = ToolStatus.CANCELLED
                self._touch_item(turn, item)
        for block in turn.blocks:
            if isinstance(block, TextBlockView) and block.streaming:
                block.streaming = False
                self._touch(turn, block)
        for entry in turn.thinking.values():
            if not entry.complete:
                entry.complete = True
                self._touch_item(turn, entry)
        self._expire(turn)
        turn.working_state = None
        if not turn.from_history:
            self.working_state = WorkingState.IDLE
        self._touch(turn)

    def _turn_output(self, turn: Turn, usage: Any) -> int | None:
        """What the model wrote in the turn: the turn's own figure when the engine gives one (a run result), else what
        the session counted since the turn began (only when that start is known: a session that had counted nothing yet
        but already had turns would put its whole past into this one)."""
        written = getattr(usage, "output_tokens", None)
        if isinstance(written, int) and written > 0:
            return written
        probe = self.output_probe
        if probe is None or not turn.out_trusted:
            return None
        try:
            now = probe()
        except Exception:
            return None
        base = turn.out_base or 0
        return now - base if now is not None and now > base else None

    # ---------------------------------------------------------------- live API

    def start_turn(
        self, text: str, attachments: Iterable[Attachment] = (), handoff: tuple[int, bool] | None = None
    ) -> Turn:
        previous = self.active_turn
        if previous is not None:
            self.finish_turn(previous, TurnStatus.INTERRUPTED)
        first = not self.turns
        turn = Turn(id=self.new_id("t"), started_at=self._clock())
        turn.ticked_at = turn.started_at
        if self.output_probe is not None:
            try:
                turn.out_base = self.output_probe()
            except Exception:
                turn.out_base = None
        turn.out_trusted = turn.out_base is not None or first
        turn.blocks.append(UserBlock(id=self.new_id("b"), text=text, attachments=self._own(attachments), handoff=handoff))
        self.turns.append(turn)
        self._touch(turn)
        return turn

    def add_steer(
        self, turn: Turn, text: str, attachments: Iterable[Attachment] = (), message_id: str | None = None, *, pending: bool = True
    ) -> UserBlock:
        """What the user said to the running turn: shown where it was said, waiting until the engine has taken it in."""
        block = UserBlock(
            id=self.new_id("b"), text=text, message_id=message_id, attachments=self._own(attachments), steer=True, pending=pending
        )
        turn.blocks.append(block)
        self._touch(turn, block)
        return block

    def settle_steer(self, turn: Turn, block: UserBlock) -> None:
        if block.pending:
            block.pending = False
            self._touch(turn, block)

    def remove_steer(self, turn: Turn, block: UserBlock) -> None:
        """The engine did not take it: it was never part of the conversation."""
        if block in turn.blocks:
            turn.blocks.remove(block)
            self._touch(turn)

    @staticmethod
    def pending_steers(turn: Turn | None) -> list[UserBlock]:
        return [] if turn is None else [b for b in turn.blocks if isinstance(b, UserBlock) and b.steer and b.pending]


    def mark_stopping(self) -> None:
        turn = self.active_turn
        if turn is not None and not turn.stopping:
            turn.stopping = True
            self._touch(turn)

    def clear_stopping(self) -> None:
        """A stop request failed: the turn goes on, so the stop button must work again."""
        turn = self.active_turn
        if turn is not None and turn.stopping:
            turn.stopping = False
            self._touch(turn)

    def fail_turn(self, message: str, *, recoverable: bool = False) -> ErrorBlock:
        block = ErrorBlock(id=self.new_id("b"), message=message, recoverable=recoverable)
        turn = self.active_turn
        if turn is None:
            self._attach_to_last_turn(block)
            return block
        self._append_block(turn, block)
        self.finish_turn(turn, TurnStatus.FAILED)
        return block

    def abort_turn(self, message: str) -> None:
        turn = self.active_turn
        if turn is None:
            return
        self._append_block(turn, NoticeBlock(id=self.new_id("b"), text=message))
        self.finish_turn(turn, TurnStatus.INTERRUPTED)

    def snapshot(self) -> list[tuple[Turn, int]]:
        """How many blocks each turn has now, for ``restore`` (an attempt that adds blocks and then fails)."""
        return [(turn, len(turn.blocks)) for turn in self.turns]

    def restore(self, snapshot: list[tuple[Turn, int]]) -> None:
        """Take back what was added since ``snapshot``: whole turns and trailing blocks of the turns that were there."""
        keep = {id(turn) for turn, _ in snapshot}
        self.turns[:] = [turn for turn in self.turns if id(turn) in keep]
        for turn, count in snapshot:
            if len(turn.blocks) > count:
                del turn.blocks[count:]
                self._touch(turn)
        self.version += 1

    def add_switch(
        self,
        *,
        from_model: str,
        from_engine: str,
        to_model: str,
        to_engine: str,
        carried: int,
        total: int,
        size: int,
        truncated: bool,
    ) -> SwitchBlock:
        """Record a change of engine as a turn of its own (finished, nothing else in it)."""
        block = SwitchBlock(
            id=self.new_id("b"),
            from_model=from_model,
            from_engine=from_engine,
            to_model=to_model,
            to_engine=to_engine,
            carried=carried,
            total=total,
            size=size,
            truncated=truncated,
        )
        turn = Turn(id=self.new_id("t"), status=TurnStatus.DONE, blocks=[block])
        self.turns.append(turn)
        self._touch(turn, block)
        return block

    def add_notice(self, text: str) -> NoticeBlock:
        block = NoticeBlock(id=self.new_id("b"), text=text)
        turn = self.active_turn
        if turn is not None:
            self._append_block(turn, block)
        else:
            self._attach_to_last_turn(block)
        return block

    def add_error(self, message: str, *, recoverable: bool = False) -> ErrorBlock:
        block = ErrorBlock(id=self.new_id("b"), message=message, recoverable=recoverable)
        turn = self.active_turn
        if turn is not None:
            self._append_block(turn, block)
        else:
            self._attach_to_last_turn(block)
        return block

    def resolve_recoverable_errors(self) -> None:
        for turn in self.turns:
            for block in turn.blocks:
                if isinstance(block, ErrorBlock) and block.recoverable and not block.resolved:
                    block.resolved = True
                    self._touch(turn, block)

    def _attach_to_last_turn(self, block: Block) -> None:
        if not self.turns:
            turn = Turn(id=self.new_id("t"), status=TurnStatus.DONE)
            self.turns.append(turn)
        turn = self.turns[-1]
        self._append_block(turn, block)

    # ---------------------------------------------------------------- prompts


    def answer_permission(self, prompt_id: str, value: str) -> bool:
        found = self._prompts.get(prompt_id)
        if found is None:
            return False
        turn, prompt = found
        if not isinstance(prompt, PermissionPrompt) or prompt.status is not PromptStatus.PENDING:
            return False
        if value not in {option.value for option in prompt.options}:
            return False
        prompt.status = PromptStatus.ANSWERED
        prompt.selected = value
        for tool_id in prompt.tool_use_ids:
            item = turn.tools.get(tool_id)
            if item is None:
                continue
            item.decision = value
            if not item.terminal:
                item.status = ToolStatus.CANCELLED if value == "cancel" else ToolStatus.RUNNING
            self._touch_item(turn, item)
        self._touch(turn, prompt)
        return True

    def answer_question(self, prompt_id: str, answers: Sequence[str] | None) -> bool:
        found = self._prompts.get(prompt_id)
        if found is None:
            return False
        turn, prompt = found
        if not isinstance(prompt, QuestionPrompt) or prompt.status is not PromptStatus.PENDING:
            return False
        if answers is not None and len(answers) != len(prompt.questions):
            return False
        prompt.status = PromptStatus.ANSWERED
        prompt.answers = None if answers is None else tuple(answers)
        if answers is None:
            pairs = [(question.question, "（已跳过）") for question in prompt.questions]
        else:
            pairs = [(question.question, answer) for question, answer in zip(prompt.questions, answers)]
        self._append_block(turn, QABlock(id=self.new_id("b"), pairs=pairs, cancelled=answers is None))
        item = turn.tools.get(prompt.tool_call_id)
        if item is not None and not item.terminal:
            item.status = ToolStatus.RUNNING
            self._touch_item(turn, item)
        self._touch(turn, prompt)
        return True

    def expire_prompt(self, prompt_id: str) -> bool:
        """Mark one still-pending prompt as expired (its handler went away); ``False`` if it was not pending."""
        found = self._prompts.get(prompt_id)
        if found is None or found[1].status is not PromptStatus.PENDING:
            return False
        turn, prompt = found
        prompt.status = PromptStatus.EXPIRED
        self._touch(turn, prompt)
        return True

    def expire_prompts(self) -> list[Prompt]:
        expired = []
        for turn, prompt in self._prompts.values():
            if prompt.status is PromptStatus.PENDING:
                prompt.status = PromptStatus.EXPIRED
                self._touch(turn, prompt)
                expired.append(prompt)
        return expired

    def _expire(self, turn: Turn) -> None:
        for prompt in turn.prompts:
            if prompt.status is PromptStatus.PENDING:
                prompt.status = PromptStatus.EXPIRED
                self._touch(turn, prompt)

    def _settle_prompts_for(self, turn: Turn, item: ToolItem) -> None:
        for prompt in turn.prompts:
            if prompt.status is not PromptStatus.PENDING:
                continue
            if isinstance(prompt, QuestionPrompt) and prompt.tool_call_id == item.tool_use_id:
                prompt.status = PromptStatus.EXPIRED
                self._touch(turn, prompt)
            elif isinstance(prompt, PermissionPrompt) and item.tool_use_id in prompt.tool_use_ids:
                prompt.status = PromptStatus.RESOLVED
                self._touch(turn, prompt)

    # ---------------------------------------------------------------- event handlers


    def _first_location(self, turn: Turn, tool_ids: list[str]) -> tuple[ActivityBlock, int] | None:
        wanted = {turn.tools[tool_id].id for tool_id in tool_ids if tool_id in turn.tools}
        if not wanted:
            return None
        for block in turn.blocks:
            if isinstance(block, ActivityBlock):
                for position, entry in enumerate(block.entries):
                    if entry.id in wanted:
                        return block, position
        return None


    # ---------------------------------------------------------------- builder API for engines without SDK events (Codex)

    def append_text(self, turn: Turn, message_id: str, block_index: int, text: str) -> TextBlockView:
        """Streamed answer text: appended to its block (created on first use), which streams until ``end_text``."""
        block = self.text_block(turn, message_id, block_index)
        block.text += text
        block.streaming = True
        self._touch(turn, block)
        return block

    def end_text(self, turn: Turn, message_id: str, block_index: int, text: str | None = None) -> TextBlockView | None:
        """The block is complete. ``text`` is the final text (``None`` or empty keeps what streamed); a block that never
        streamed is only created when ``text`` has content."""
        block = turn.texts.get((message_id, block_index))
        if block is None:
            if not text or not text.strip():
                return None
            block = self.text_block(turn, message_id, block_index)
        if text:
            block.text = text
        block.streaming = False
        self._touch(turn, block)
        return block

    def append_thinking(self, turn: Turn, message_id: str, block_index: int, text: str) -> ThinkingEntry:
        entry = self.thinking_entry(turn, message_id, block_index)
        entry.text += text
        self._touch_item(turn, entry)
        return entry

    def end_thinking(
        self, turn: Turn, message_id: str, block_index: int, *, text: str | None = None, duration: float | None = None
    ) -> ThinkingEntry | None:
        """Like ``end_text`` for a thinking entry (only created when ``text`` has content)."""
        entry = turn.thinking.get((message_id, block_index))
        if entry is None:
            if not text or not text.strip():
                return None
            entry = self.thinking_entry(turn, message_id, block_index)
        if text:
            entry.text = text
        if duration is not None:
            entry.duration = duration
        entry.complete = True
        self._touch_item(turn, entry)
        return entry

    def tool_running(self, turn: Turn, item: ToolItem, output: str | None = None) -> None:
        """The tool is at work; ``output`` is everything it printed so far (the tail is kept)."""
        if output:
            item.progress = output[-PROGRESS_TAIL_CHARS:]
        if not item.terminal:
            item.status = ToolStatus.RUNNING
            if item.started_at is None:
                item.started_at = self._clock()
        self._touch_item(turn, item)

    def set_tool_diffs(self, turn: Turn, item: ToolItem, diffs: Sequence[FileDiff], *, source: str = "input") -> None:
        item.diffs = tuple(diffs)
        item.diff_source = source
        self._touch_item(turn, item)

    def finish_tool(self, turn: Turn, item: ToolItem, text: str, *, status: ToolStatus, at: float | None = None) -> None:
        """The tool ended the way the engine says (``record_result`` infers the outcome from a result's text instead)."""
        if item.result is not None:
            item.duplicate_results += 1
            self._touch_item(turn, item)
            return
        item.result = text
        item.status = status if status in TERMINAL_TOOL_STATUSES else ToolStatus.DONE
        item.is_error = item.status is ToolStatus.ERROR
        item.finished_at = self._clock() if at is None else at
        self._settle_prompts_for(turn, item)
        self._accept_tool_plan(turn, item)
        self._touch_item(turn, item)

    def add_permission_prompt(
        self, tool_use_ids: Sequence[str], actions: Sequence[ActionView], options: Sequence[OptionView]
    ) -> PermissionPrompt | None:
        """``add_permission`` from display data instead of an SDK request; the tools must already be in the active turn."""
        turn = self.active_turn
        if turn is None:
            return None
        ids = tuple(tool_id for tool_id in tool_use_ids if tool_id in turn.tools)
        prompt = PermissionPrompt(id=self.new_id("p"), tool_use_ids=ids, actions=tuple(actions), options=tuple(options))
        stale = bool(ids) and all(turn.tools[tool_id].terminal for tool_id in ids)
        if stale:
            prompt.status = PromptStatus.EXPIRED
        for view in prompt.actions:
            item = turn.tools.get(view.tool_use_id)
            if item is not None and view.diffs and item.diff_source != "result":
                item.diffs = view.diffs
                item.diff_source = "permission"
        for tool_id in ids:
            item = turn.tools[tool_id]
            if not stale and not item.terminal:
                item.status = ToolStatus.AWAITING_PERMISSION
            self._touch_item(turn, item)
        turn.prompts.append(prompt)
        self._prompts[prompt.id] = (turn, prompt)
        self._touch(turn, prompt)
        return prompt

    def add_question_prompt(self, tool_call_id: str, questions: Sequence[QuestionView], *, who: str = "") -> QuestionPrompt | None:
        """``add_question`` from display data; the questions belong to an ``AskUser`` item of the active turn."""
        turn = self.active_turn
        if turn is None:
            return None
        item = self.ensure_tool(turn, tool_call_id, "AskUser")
        prompt = QuestionPrompt(id=self.new_id("p"), tool_call_id=tool_call_id, questions=tuple(questions), who=who)
        if item.terminal:
            prompt.status = PromptStatus.EXPIRED
        else:
            item.status = ToolStatus.AWAITING_PERMISSION
        self._touch_item(turn, item)
        turn.prompts.append(prompt)
        self._prompts[prompt.id] = (turn, prompt)
        self._touch(turn, prompt)
        return prompt

    def set_working_state(self, state: str | None) -> None:
        """What the running turn is doing, as a ``WorkingState`` value (the labels the page already has)."""
        try:
            self.working_state = WorkingState(state) if state else None
        except ValueError:
            self.working_state = None
        turn = self.active_turn
        if turn is not None and turn.working_state != state:
            turn.working_state = state
            self._touch(turn)

    def append_error(self, turn: Turn, message: str, *, recoverable: bool = False) -> ErrorBlock:
        """An error block at the end of ``turn``, whether or not it is the active one (e.g. a turn rebuilt from history)."""
        block = ErrorBlock(id=self.new_id("b"), message=message, recoverable=recoverable)
        self._append_block(turn, block)
        return block

    def add_history_turns(self, turns: Sequence[Turn]) -> int:
        """Put turns rebuilt from another engine's saved history (``new_history_turn`` ...) before the others."""
        self.turns[0:0] = list(turns)
        self.version += 1
        return len(turns)