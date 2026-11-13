"""The agent loop: ask the model, run the tools it calls, give it the results, until it answers without a tool call.

The loop works on Responses items (``core/chatgpt/responses``): the history is a list of them, each model answer is
appended as it finishes, each tool result as a ``function_call_output``. It knows no UI and no provider: the model is a
function from a request to a stream of events, the tools are a ``Tools`` object, and everything the page should show goes
to an ``Observer``.

Invariants worth knowing:

- Every ``function_call`` in the history has its ``function_call_output`` before the next request is made, also when the
  turn is cancelled halfway (the missing outputs are written as "interrupted"): the API refuses a history with a call that
  has no output, and the conversation could never go on.
- A step is one request. A turn ends when a step brings no tool call and nothing the user said meanwhile is waiting
  (``Observer.take_steer``), or at the configured step budget (default ``MAX_STEPS``).
- A request that fails before any of its answer arrived, in a way that may pass (the connection broke, the server is busy or
  overloaded: ``errors.is_transient``), is sent again after a pause that grows (``RETRY_DELAYS``, or the server's own
  ``Retry-After``), and the user is told. Nothing of the failed attempt is in the history, so the repeat is the same request.
- A tool never raises into the loop: its failure is its result, which the model reads and reacts to.
- Whatever the model wrote as the arguments of a call is checked here (valid JSON, an object); the tool checks the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from desktop.core.chatgpt.errors import ChatGPTError, is_transient
from desktop.core.chatgpt.responses import (
    CallBegun,
    CallDone,
    Finished,
    ItemDone,
    ReasoningDelta,
    ReasoningDone,
    Shape,
    Started,
    TextDelta,
    TextDone,
    Usage,
    build_request,
    call_output,
    history_item,
    user_message,
)

from desktop.core.harness.compact import estimate_item, estimate_items, estimate_text, fixed_tokens, is_overflow

log = logging.getLogger("desktop.harness")

MAX_STEPS = 80
RETRY_DELAYS = (1.5, 4.0, 10.0, 25.0)
"""Seconds to wait before each repeat of a request that failed in a way that may pass; four repeats at most."""
RETRY_LONGEST = 60.0
"""The longest pause a server's ``Retry-After`` is obeyed for (a turn that waits longer is better stopped and said so)."""
OUTPUT_LIMIT = 60_000
"""A tool result longer than this is cut (head and tail kept) before the model sees it."""
MAX_PARALLEL_READS = 4
OUTPUT_FLOOR = 1_000
"""Tokens one tool result keeps however little room the next request has left (``Observer.tool_room``)."""
REPEAT_AFTER = 3
"""The call that is the same as the one before it this many times in a row gets a reminder with its result."""
REPEATED = (
    "\n\n[Reminder: you have now called {name} with exactly these arguments {count} times in a row. Repeating it will not give a "
    "different result. Change your approach (other arguments, another tool, a closer look at the error), or stop and tell the user "
    "what is blocking you.]"
)
INTERRUPTED = "The user interrupted this turn before the tool ran or finished; its result is unknown."
DECLINED = "The user declined this action. Do not try again unless they ask; say what you could not do and why."

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass(slots=True)
class Call:
    """One tool call the model made."""

    item_id: str
    call_id: str
    name: str
    raw: str
    args: dict[str, Any] = field(default_factory=dict)
    problem: str = ""
    """Why the arguments are unusable (not JSON, not an object); the model is told and nothing runs."""


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str
    reason: str = ""


@dataclass(slots=True)
class Outcome:
    output: str
    is_error: bool = False
    diffs: tuple[Any, ...] = ()
    cancelled: bool = False
    """The user said no (or the turn was stopped): shown as cancelled rather than failed."""


@dataclass(frozen=True, slots=True)
class Steer:
    """Something the user said while the turn ran, to be given to the model at its next step."""

    text: str
    images: tuple[str, ...] = ()
    ref: Any = None
    """The caller's handle on it (the block in the page)."""
    item: dict[str, Any] | None = None
    """The message as an item, when the caller built it (with files and pictures); else made from ``text`` and ``images``."""


class Tools(Protocol):
    def declarations(self) -> list[dict[str, Any]]:
        """``{name, description, parameters}`` of every tool the model may call."""

    def known(self, name: str) -> bool: ...

    def check(self, call: Call) -> Verdict:
        """May the call run now, must the user be asked, or is it refused outright."""

    def remember(self, call: Call) -> None:
        """The user allowed this kind of call for the rest of the conversation."""

    async def run(self, call: Call) -> Outcome: ...


class Observer:
    """What the loop reports; the default does nothing. The controller overrides what the page shows."""

    def working(self, state: str | None) -> None: ...

    def text_delta(self, item_id: str, text: str) -> None: ...

    def text_done(self, item_id: str, text: str) -> None: ...

    def thinking_delta(self, item_id: str, text: str) -> None: ...

    def thinking_done(self, item_id: str, text: str) -> None: ...

    def call_begun(self, item_id: str, call_id: str, name: str) -> None: ...

    def call_ready(self, call: Call) -> None:
        """The arguments are complete (or unusable: ``call.problem``)."""

    async def ask(self, call: Call, verdict: Verdict) -> str:
        """Ask the user about ``call``: ``proceed_once``, ``proceed_always`` or anything else for no."""
        return "cancel"

    def call_running(self, call: Call) -> None: ...

    def call_done(self, call: Call, outcome: Outcome) -> None: ...

    def usage(self, usage: Usage) -> None: ...

    def request_done(self, request: dict[str, Any], finished: Finished | None, seconds: float, failed: bool) -> None:
        """A request ended (also a failed attempt); observers must not persist raw request/response content as diagnostics."""

    def history_grew(self, items: Sequence[dict[str, Any]]) -> None:
        """Items were appended to the history (to be saved)."""

    async def prepare(self, history: list[dict[str, Any]], estimate: int, overflow: bool) -> bool:
        """Asked before every request. ``estimate``: the tokens the request would hold; ``overflow``: the server has just
        refused a request as too big. The observer may replace what ``history`` holds, in place (a compaction, see
        ``harness.compact``); ``True`` when it did."""
        return False

    def tool_room(self, estimate: int, step: int) -> int | None:
        """How many tokens the results of the tool calls about to run may hold together, for the request that carries
        them to stay one that can be sent. ``estimate``: the tokens of that request without them (as ``prepare`` is told);
        ``step``: those of the model's answer that made the calls. ``None``: no limit but that of one result
        (``OUTPUT_LIMIT``)."""
        return None

    def has_steer(self) -> bool:
        """Is something the user said waiting to be given to the model? (Asked when a step brought no tool call.)"""
        return False

    def take_steer(self) -> list[Steer]:
        return []

    def steer_given(self, steer: Steer) -> None:
        """The model will see this at its next request."""

    def notice(self, text: str) -> None: ...


Stream = Callable[[dict[str, Any]], AsyncIterator[Any]]


@dataclass(slots=True)
class TurnEnd:
    reason: str
    """``done`` | ``incomplete`` | ``step_limit``."""
    detail: str = ""
    steps: int = 0


def clip_output(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters omitted ...]\n{text[-tail:]}"


_SHARED = (
    "\n[... {omitted} characters omitted: the results of this step had to share what is left of the context window. Ask "
    "for a smaller part to see more ...]\n"
)


def clip_tokens(text: str, tokens: int) -> str:
    """``text`` cut in the middle (head and tail kept) to at most about ``tokens`` (``estimate_text``), saying why: the
    results of one step share the room the next request has (``Observer.tool_room``)."""
    if estimate_text(text) <= tokens:
        return text

    def kept(keep: int) -> str:
        head = keep * 2 // 3
        tail = text[len(text) - (keep - head) :] if keep > head else ""
        return text[:head] + _SHARED.format(omitted=len(text) - keep) + tail

    low, high = 0, len(text) - 1
    while low < high:  # (the most characters that fit: the head and the tail may be denser than the text on the whole)
        middle = (low + high + 1) // 2
        if estimate_text(kept(middle)) <= tokens:
            low = middle
        else:
            high = middle - 1
    return kept(low)


def clip_read_output(text: str, tokens: int) -> str:
    """A read page is a consecutive line range, never a head/tail splice with a misleading EOF footer."""
    if estimate_text(text) <= tokens:
        return text
    numbered = [(int(match[1]), line) for line in text.splitlines() if (match := re.match(r"^\s*(\d+)\t", line))]
    if not numbered:
        return clip_tokens(text, tokens)
    kept: list[str] = []
    used = 0
    last = numbered[0][0] - 1
    for number, line in numbered:
        cost = estimate_text(line + "\n")
        if used + cost > max(tokens - 120, 0):
            break
        kept.append(line)
        used += cost
        last = number
    if not kept:
        return f"No file lines fit in this step's remaining context. Read a smaller part with offset={last + 1}, limit=1, or use grep; a very long single line may need a shell command."
    return "\n".join(kept) + f"\n[Showing lines {numbered[0][0]}-{last} only: the results of this step had to share what is left of the context window. Later lines were not shown. Call read_file again with offset={last + 1} to continue.]"


_OUTPUT_ITEM = estimate_item(call_output("", "")) - estimate_text("")
"""What a ``function_call_output`` costs besides its text."""


def parse_call(done: CallDone) -> Call:
    call = Call(done.item_id, done.call_id, done.name, done.arguments)
    text = done.arguments.strip() or "{}"
    try:
        parsed = json.loads(text)
    except ValueError as error:
        call.problem = f"The arguments are not valid JSON ({error}). Send a JSON object."
        return call
    if not isinstance(parsed, dict):
        call.problem = "The arguments must be a JSON object."
        return call
    call.args = parsed
    return call


class AgentLoop:
    def __init__(
        self,
        stream: Stream,
        tools: Tools,
        observer: Observer,
        *,
        model: str,
        reasoning: str | None,
        instructions: str,
        shape: Shape = Shape(),
        conversation: str = "",
        max_steps: int | Callable[[], int] = MAX_STEPS,
        measured: tuple[int, int] | None = None,
        floor: int = 0,
        retry_delays: Sequence[float] | None = None,
        pause: Callable[[float], Awaitable[object]] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.stream = stream
        self._retry_delays = tuple(RETRY_DELAYS if retry_delays is None else retry_delays)
        self._pause = pause or asyncio.sleep
        self._jitter = jitter or random.random
        self.tools = tools
        self.observer = observer
        self.model = model
        self.reasoning = reasoning
        self.instructions = instructions
        self.shape = shape
        self.conversation = conversation
        self.max_steps = max_steps
        self._measured: tuple[int, int] | None = measured
        """``(len(history), tokens)`` as of the last response: its input and its answer, which is all of the history then.
        A turn starts with the figure the last turn ended with when the caller knows it is still true of this history."""
        self._floor = floor
        """Tokens the conversation was known to hold at some earlier time, by another model's count or before a restart: not
        exact for this request, but it cannot have shrunk since, so the estimate is never below it."""
        self._events = 0
        self._acknowledged = False
        self._answer = 0
        """Tokens of the items the last response added to the history."""
        self._last_call = ""
        self._repeats = 0
        """How many calls in a row were exactly the one before (``reminder``)."""

    @property
    def measured(self) -> tuple[int, int] | None:
        """``(items, tokens)`` of the last response, as the next turn may start from it."""
        return self._measured

    def estimate(self, history: Sequence[dict[str, Any]]) -> int:
        """Tokens of the next request: the last measure plus an estimate of what came after it (all estimated when there
        is no measure yet, instructions and tool declarations included)."""
        if self._measured is not None and self._measured[0] <= len(history):
            return self._measured[1] + estimate_items(history[self._measured[0] :])
        return max(estimate_items(history) + fixed_tokens(self.instructions, self.tools.declarations(), self.shape), self._floor)

    async def _prepare(self, history: list[dict[str, Any]], overflow: bool = False) -> bool:
        replaced = await self.observer.prepare(history, self.estimate(history), overflow)
        if replaced:
            self._measured = None
            self._floor = 0
        return replaced

    async def run(self, history: list[dict[str, Any]]) -> TurnEnd:
        """Run steps on ``history`` (which already ends with the user's message) until the turn is over. ``history`` is
        extended in place; the exception ``asyncio.CancelledError`` leaves it valid (see the module docstring). Before a
        request the observer may shorten ``history`` (``Observer.prepare``)."""
        incomplete = ""
        step = 0
        recovered_empty = False
        # A callable lets the app apply a saved change to an already running turn, without interrupting a request or tool.
        while step < (self.max_steps() if callable(self.max_steps) else self.max_steps):
            step += 1
            self._deliver_steers(history)
            await self._prepare(history)
            calls, incomplete = await self._step(history)
            if not calls:
                if self.observer.has_steer():
                    continue
                if not incomplete and not self._visible_text:
                    if recovered_empty:
                        return TurnEnd("incomplete", "empty_response", step)
                    recovered_empty = True
                    update = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": (
                        "Your last response had no visible answer and no tool call. Give the user an answer now, or call "
                        "a tool if work remains. Do not repeat completed actions."
                    )}]}
                    history.append(update)
                    self.observer.history_grew([update])
                    continue
                return TurnEnd("incomplete" if incomplete else "done", incomplete, step)
            await self._run_calls(history, calls)
        return TurnEnd("step_limit", steps=step)

    # ------------------------------------------------------------------ a step

    def _deliver_steers(self, history: list[dict[str, Any]]) -> None:
        steers = self.observer.take_steer()
        if not steers:
            return
        items = [steer.item if steer.item is not None else user_message(steer.text, steer.images) for steer in steers]
        history.extend(items)
        self.observer.history_grew(items)
        for steer in steers:
            self.observer.steer_given(steer)

    def _request(self, history: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return build_request(
            model=self.model,
            instructions=self.instructions,
            items=history,
            tools=self.tools.declarations(),
            reasoning=self.reasoning,
            shape=self.shape,
            conversation=self.conversation,
        )

    async def _step(self, history: list[dict[str, Any]]) -> tuple[list[Call], str]:
        repeats = 0
        shortened = False
        while True:
            try:
                return await self._read(history, self._request(history))
            except Exception as error:  # (a stop is a CancelledError and passes)
                if self._events:
                    raise  # (part of the answer was shown and acted on: asking again would not be the same request)
                if is_overflow(error):
                    # Refused as too big before the answer began: the observer may be able to shorten the history; once, then it is the error.
                    log.warning("the request was refused as too big (%s); compacting and asking again", error)
                    if shortened or not await self._prepare(history, overflow=True):
                        raise
                    shortened = True
                elif self._acknowledged and getattr(error, "code", None) in ("network", "stream", "stream_idle", "timeout"):
                    raise  # no blind resend after an accepted request goes silent; an explicit busy refusal can retry
                elif repeats < len(self._retry_delays) and is_transient(error):
                    delay = self._retry_pause(repeats, error)
                    repeats += 1
                    log.warning("the request failed (%s); asking again in %.1f s (%d of %d)", error, delay, repeats, len(self._retry_delays))
                    self.observer.notice(f"{self._why(error)}，{delay:.0f} 秒后自动重试（第 {repeats}/{len(self._retry_delays)} 次）。")
                    await self._pause(delay)
                else:
                    raise

    def _retry_pause(self, repeats: int, error: BaseException) -> float:
        base = self._retry_delays[repeats]
        asked = getattr(error, "retry_after", None)
        if isinstance(asked, (int, float)) and asked > 0:
            return min(max(float(asked), base), RETRY_LONGEST)
        return base * (1 + 0.25 * self._jitter())

    @staticmethod
    def _why(error: BaseException) -> str:
        status = getattr(error, "status", None)
        code = str(getattr(error, "code", "") or "")
        if status == 429 or "rate" in code.lower() or "throttl" in code.lower():
            return "请求太频繁" + (f"（HTTP {status}）" if status else "")
        if code in ("network", "stream"):
            return "连接中断了" if code == "network" else "回答被截断了"
        return "服务暂时出错" + (f"（HTTP {status}）" if status else "")

    async def _read(self, history: list[dict[str, Any]], request: dict[str, Any]) -> tuple[list[Call], str]:
        self.observer.working("thinking")
        kept: list[dict[str, Any]] = []
        calls: list[Call] = []
        incomplete = ""
        context = 0
        self._events = 0
        self._acknowledged = False
        finished: Finished | None = None
        started = time.monotonic()
        texts: dict[str, str] = {}
        self._visible_text = False
        failed = True
        try:
            async for event in self.stream(request):
                if isinstance(event, Started):
                    self._acknowledged = True
                elif not isinstance(event, Finished):
                    self._events += 1
                if isinstance(event, TextDelta):
                    texts[event.item_id] = texts.get(event.item_id, "") + event.text
                    self.observer.text_delta(event.item_id, event.text)
                elif isinstance(event, TextDone):
                    texts[event.item_id] = event.text
                    self.observer.text_done(event.item_id, event.text)
                elif isinstance(event, ReasoningDelta):
                    self.observer.thinking_delta(event.item_id, event.text)
                elif isinstance(event, ReasoningDone):
                    self.observer.thinking_done(event.item_id, event.text)
                elif isinstance(event, CallBegun):
                    self.observer.call_begun(event.item_id, event.call_id, event.name)
                elif isinstance(event, CallDone):
                    call = parse_call(event)
                    calls.append(call)
                    self.observer.call_ready(call)
                elif isinstance(event, ItemDone):
                    item = history_item(event.item)
                    if item is not None:
                        kept.append(item)
                elif isinstance(event, Finished):
                    finished = event
                    incomplete = event.incomplete
                    if event.usage is not None:
                        context = event.usage.context
                        self.observer.usage(event.usage)
            self._visible_text = any(text.strip() for text in texts.values())
            failed = False
        finally:
            if failed:
                # Completed output is useful even if the response never finished. Preserve visible partial text, but
                # never execute an incomplete call. Completed calls receive an explicit unknown/interrupted result.
                for item_id, text in texts.items():
                    if text and not any(item.get("type") == "message" and (
                        item.get("id") == item_id or "".join(str(p.get("text") or "") for p in item.get("content") or ()) == text
                    ) for item in kept):
                        kept.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
                kept.extend(call_output(str(item.get("call_id") or ""), INTERRUPTED) for item in list(kept) if item.get("type") == "function_call")
            history.extend(kept)
            self._answer = estimate_items(kept)
            if context and not failed:
                self._measured = (len(history), context)
            if kept:
                self.observer.history_grew(kept)
            self.observer.request_done(request, finished, time.monotonic() - started, failed)
        return calls, incomplete

    # ------------------------------------------------------------------ tools

    async def _run_calls(self, history: list[dict[str, Any]], calls: list[Call]) -> None:
        answered: set[str] = set()
        # (the results of one step share the room the next request has: one long result must not crowd out the others,
        # nor all of them together make a request that cannot be sent)
        room = self.observer.tool_room(self.estimate(history), self._answer)
        outcomes: dict[int, Outcome] = {}
        tasks: list[asyncio.Task[Any]] = []
        safe = getattr(self.tools, "parallel_safe", lambda call: False)

        def commit(index: int, call: Call, outcome: Outcome) -> None:
            nonlocal room
            self.observer.call_done(call, outcome)
            output = clip_output(outcome.output) + self.reminder(call, outcome)
            if room is not None:
                clip = clip_read_output if call.name == "read_file" and not outcome.is_error else clip_tokens
                output = clip(output, max(OUTPUT_FLOOR, room // (len(calls) - index)) - _OUTPUT_ITEM)
            item = call_output(call.call_id, output)
            if room is not None:
                room = max(0, room - estimate_item(item))
            history.append(item)
            answered.add(call.call_id)
            self.observer.history_grew([item])

        async def execute(index: int) -> None:
            outcomes[index] = await self._execute(calls[index])

        try:
            index = 0
            while index < len(calls):
                end = index + 1
                if safe(calls[index]):
                    while end < min(index + MAX_PARALLEL_READS, len(calls)) and safe(calls[end]):
                        end += 1
                tasks = [asyncio.create_task(execute(position)) for position in range(index, end)]
                await asyncio.gather(*tasks)
                for position in range(index, end):
                    commit(position, calls[position], outcomes.pop(position))
                tasks = []
                index = end
        except BaseException:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Keep completed concurrent reads, and pair every unfinished call without executing it again.
            for index, call in enumerate(calls):
                if call.call_id in answered:
                    continue
                outcome = outcomes.get(index)
                if outcome is not None:
                    commit(index, call, outcome)
                else:
                    item = call_output(call.call_id, INTERRUPTED)
                    history.append(item)
                    self.observer.history_grew([item])
            raise

    def reminder(self, call: Call, outcome: Outcome) -> str:
        """What is added to a result when the model has made exactly this call ``REPEAT_AFTER`` times in a row (advisory: the call
        was made, and the reminder only asks the model to change course). A stuck loop spends time and tokens for nothing; the
        idea is that of DeepSeek Harness's repeat-tool reminder. A call the user declined says so already."""
        key = f"{call.name}\0{json.dumps(call.args, sort_keys=True, default=str)}"
        if key == self._last_call:
            self._repeats += 1
        else:
            self._last_call, self._repeats = key, 1
        if self._repeats < REPEAT_AFTER or outcome.cancelled:
            return ""
        return REPEATED.format(name=call.name, count=self._repeats)

    async def _execute(self, call: Call) -> Outcome:
        if call.problem:
            return Outcome(call.problem, True)
        if not self.tools.known(call.name):
            return Outcome(f"There is no tool named {call.name!r}. Use one of: {', '.join(d['name'] for d in self.tools.declarations())}.", True)
        verdict = self.tools.check(call)
        if verdict.verdict == DENY:
            return Outcome(f"Refused: {verdict.reason}" if verdict.reason else "Refused.", True, cancelled=True)
        if verdict.verdict == ASK:
            self.observer.working("waiting_for_tool_confirmation")
            answer = await self.observer.ask(call, verdict)
            if answer not in ("proceed_once", "proceed_always"):
                return Outcome(DECLINED, True, cancelled=True)
            if answer == "proceed_always":
                self.tools.remember(call)
        self.observer.working("running_tool")
        self.observer.call_running(call)
        try:
            return await self.tools.run(call)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # a tool's bug is the model's information, not the end of the conversation
            log.exception("tool %s failed", call.name)
            return Outcome(f"The tool failed unexpectedly: {type(error).__name__}: {error}", True)


