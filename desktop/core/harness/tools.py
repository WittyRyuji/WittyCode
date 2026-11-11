"""The tool contract of the harness: what a tool is, what it runs in, what it returns, and the registry that runs them.

``ToolRegistry.run`` never raises for a tool's own failure. A bad argument, a missing file, a refused path, an
unexpected exception in a handler: all come back as ``ToolResult(is_error=True)`` whose text the model can act on. Only
cancellation (``asyncio.CancelledError``) passes through, so that stopping a turn still stops the tool.

A sync handler runs in a worker thread, and a thread cannot be cancelled. So a cancelled call stops the call's
``writes`` guard, sets its ``abort`` and waits up to ``THREAD_WAIT_SECONDS`` for the thread to return before passing
the cancellation on. The file tools commit every write under that guard, so a write that has not begun when the call
is stopped never happens, however late the thread gets there; one that has begun cannot be taken back, and is logged.
The handler gets its own copy of the context, because the controller gives every turn a new ``abort`` event and a
thread still running from a stopped turn must keep seeing the old (set) one.

Arguments are validated against the tool's JSON schema first (required keys, types, ``enum``, ``minimum`` /
``maximum``, ``minItems``). ``null`` counts as absent (models send it for optional parameters), unknown keys are
dropped, an integral float is accepted as an integer. Nothing else is coerced: ``"5"`` is not an integer.

The page shows every call as a Droid-style item (``Read``, ``LS``, ``Glob``, ``Grep``, ``Create``, ``Edit``,
``Execute``) and reads its ``input`` keys (``file_path``, ``directory_path``, ``patterns``, ``folder`` ...) in
``toolinfo.describe_input``. ``ui_input`` maps the model's arguments to those keys and keeps only what the model
actually passed, so the page never shows a default the model did not choose.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from desktop.core.diffs import FileDiff

log = logging.getLogger("desktop.harness.tools")

THREAD_WAIT_SECONDS = 5.0


class ToolError(Exception):
    """A failure the model can act on: the message becomes the tool result."""


STOPPED_NOTHING_WRITTEN = "Stopped by the user; nothing was written."


class CommitGuard:
    """Stopping a call and committing its write are one decision, taken under one lock.

    ``stop`` refuses a write that has not begun. It cannot refuse one that has: ``os.replace`` is not undone by
    anything, so when a worker thread is already inside the commit, ``stop`` answers False and the file may hold the
    new content. The turn reports every interrupted call as having an unknown result (``loop.INTERRUPTED``), which
    stays true in that case.
    """

    __slots__ = ("_lock", "stopped", "writing", "written")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stopped = False
        self.writing = False
        self.written = False

    def stop(self) -> bool:
        """Ask the call to write nothing. True when nothing was written and nothing is being written, so that
        "stopped" is the truth; False when a write is under way or done and the outcome is unknown."""
        with self._lock:
            self.stopped = True
            return not (self.writing or self.written)

    @contextlib.contextmanager
    def commit(self, abort: threading.Event) -> Iterator[Callable[[], None]]:
        """The window around one write. Raises ``ToolError`` when the call was stopped before the window opened; the
        callback it yields records that the write went through, which no later ``stop`` may contradict."""
        with self._lock:
            if self.stopped or abort.is_set():
                raise ToolError(STOPPED_NOTHING_WRITTEN)
            self.writing = True
        try:
            yield self._record
        finally:
            with self._lock:
                self.writing = False

    def _record(self) -> None:
        with self._lock:
            self.written = True


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    ui_name: str
    kind: str  # "read" | "edit" | "exec"


@dataclass
class ToolContext:
    """What a tool runs in. ``protected`` paths (settings, credentials) are refused by the tools themselves, whatever
    the permission level. ``writes`` is per call (``_in_thread`` gives every call its own), so stopping one call cannot
    refuse another call's write."""

    project: Path
    abort: threading.Event = field(default_factory=threading.Event)
    shell: str = "powershell"
    protected: tuple[Path, ...] = ()
    readable: tuple[Path, ...] = ()
    """Folders outside the project that reads may enter without asking (the folders of the user's skills, ``harness.skills``).
    Only reads: nothing here may be written, and ``protected`` still wins."""
    writes: CommitGuard = field(default_factory=CommitGuard)


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    diffs: tuple[FileDiff, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)  # the page's ToolItem input, i.e. ``ui_input``'s result


Handler = Callable[[dict[str, Any], ToolContext], "ToolResult | Awaitable[ToolResult]"]
UiInput = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class _Entry:
    spec: ToolSpec
    handler: Handler
    ui_input: UiInput
    is_async: bool


def _coerce(value: Any, schema: Mapping[str, Any]) -> tuple[Any, str | None]:
    kind = schema.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return value, "must be a string"
    elif kind == "integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            return value, "must be an integer"
        if "minimum" in schema and value < schema["minimum"]:
            return value, f"must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return value, f"must be at most {schema['maximum']}"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return value, "must be true or false"
    elif kind == "array":
        if not isinstance(value, (list, tuple)):
            return value, "must be an array"
        items = schema.get("items", {})
        converted = []
        for item in value:
            item, problem = _coerce(item, items)
            if problem:
                return value, f"must be an array of {items.get('type', 'values')}s"
            converted.append(item)
        if len(converted) < schema.get("minItems", 0):
            return value, f"needs at least {schema['minItems']} item(s)"
        value = converted
    if "enum" in schema and value not in schema["enum"]:
        return value, "must be one of " + ", ".join(repr(option) for option in schema["enum"])
    return value, None


def validate_arguments(schema: Mapping[str, Any], args: Any) -> dict[str, Any]:
    """The arguments the handler gets, or a ``ToolError`` naming every problem."""
    if not isinstance(args, Mapping):
        raise ToolError("The arguments must be a JSON object.")
    properties = schema.get("properties", {})
    given = {key: value for key, value in args.items() if value is not None}
    problems = [f"'{key}' is required" for key in schema.get("required", ()) if key not in given]
    result: dict[str, Any] = {}
    for key, value in given.items():
        prop = properties.get(key)
        if prop is None:
            continue
        value, problem = _coerce(value, prop)
        if problem:
            problems.append(f"'{key}' {problem}")
        else:
            result[key] = value
    if problems:
        raise ToolError("; ".join(problems) + ".")
    return result


async def _in_thread(handler: Handler, args: dict[str, Any], ctx: ToolContext) -> Any:
    call = replace(ctx, writes=CommitGuard())
    work = asyncio.ensure_future(asyncio.to_thread(handler, args, call))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        if not call.writes.stop():
            log.warning("a call was stopped after its write had begun; the file may hold the new content")
        call.abort.set()
        work.add_done_callback(_retrieve)
        deadline = time.monotonic() + THREAD_WAIT_SECONDS
        while not work.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({work}, timeout=remaining)
            except asyncio.CancelledError:
                continue  # another stop request: the thread still gets the rest of its time
        raise


def _retrieve(work: asyncio.Future[Any]) -> None:
    """Read the outcome of a call nobody awaits any more, so that its failure is not logged as never retrieved."""
    if not work.cancelled():
        work.exception()


def passthrough_input(keys: Mapping[str, str]) -> UiInput:
    """``ui_input`` that renames the model's argument keys to the page's and drops what the model did not pass."""

    def build(args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            return {}
        return {page: args[model] for model, page in keys.items() if args.get(model) is not None}

    return build


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    @classmethod
    def default(cls) -> ToolRegistry:
        from desktop.core.harness import fs_tools, shell, skill_tool, todo

        registry = cls()
        fs_tools.register(registry)
        shell.register(registry)
        todo.register(registry)
        skill_tool.register(registry)
        return registry

    def register(self, spec: ToolSpec, handler: Handler, *, ui_input: UiInput, is_async: bool = False) -> None:
        """Add a tool. A sync handler runs in a worker thread so that file and search work never blocks the loop."""
        if spec.name in self._entries:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._entries[spec.name] = _Entry(spec, handler, ui_input, is_async)

    def specs(self) -> list[ToolSpec]:
        return [entry.spec for entry in self._entries.values()]

    def get(self, name: str) -> ToolSpec | None:
        entry = self._entries.get(name)
        return entry.spec if entry else None

    def ui_input(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._entries.get(name)
        if entry is None:
            return dict(args) if isinstance(args, Mapping) else {}
        try:
            return entry.ui_input(args)
        except Exception:
            return {}

    async def run(self, name: str, args: Any, ctx: ToolContext) -> ToolResult:
        entry = self._entries.get(name)
        if entry is None:
            known = ", ".join(sorted(self._entries))
            return ToolResult(f"Unknown tool {name!r}. Available tools: {known}.", is_error=True)
        shown = self.ui_input(name, args)
        try:
            clean = validate_arguments(entry.spec.parameters, args)
        except ToolError as error:
            return ToolResult(f"Invalid arguments for {name}: {error}", is_error=True, data=shown)
        try:
            if entry.is_async:
                result = await entry.handler(clean, ctx)
            else:
                result = await _in_thread(entry.handler, clean, ctx)
            if not isinstance(result, ToolResult):
                raise TypeError(f"the handler returned {type(result).__name__}, not a ToolResult")
            result.data = {**shown, **result.data}
        except ToolError as error:
            return ToolResult(str(error), is_error=True, data=shown)
        except Exception as error:
            return ToolResult(f"{name} failed: {type(error).__name__}: {error}", is_error=True, data=shown)
        return result
