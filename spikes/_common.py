"""Shared helpers for the WittyCode Phase 0 spikes.

Paths, Droid runtime construction, a JSONL event recorder with a generic
serializer, a compact console timeline, and a sandbox-only permission policy.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  (aborts unless Python 3.11.x)

import dataclasses
import datetime as dt
import enum
import json
import math
import os
import platform
import re
import sys
import time
from collections.abc import Callable, Mapping, Set
from pathlib import Path, PurePath
from typing import Any

import droid_sdk
from droid_sdk import (
    ApplyPatchAction,
    AskUserAction,
    AssistantMessage,
    CreateFile,
    EditAction,
    ErrorEvent,
    ExecuteAction,
    PermissionRequest,
    PermissionResolved,
    PermissionResponse,
    QuestionRequest,
    QuestionResponse,
    ReasoningEffort,
    RunResult,
    Runtime,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    TokenUsageUpdate,
    ToolCall,
    ToolCallDelta,
    ToolConfirmationOutcome,
    ToolProgress,
    ToolResult,
    UserMessage,
    WorkingStateChanged,
)

from find_droid import droid_version, find_droid

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox"
FIXTURES = ROOT / "fixtures"

# Chosen from list_models.py output: lowest token multiplier (0.04) among enabled models.
# "low" keeps cost down; at this level Haiku 5.5 emits thinking only in some turns.
DEFAULT_MODEL = os.environ.get("WITTYCODE_MODEL") or "claude-haiku-5-5"
DEFAULT_REASONING = os.environ.get("WITTYCODE_REASONING") or "low"

SANDBOX_README = (
    "This folder is a throwaway sandbox for WittyCode Phase 0 spikes.\n"
    "Agent turns started by the spike scripts use this folder as their working directory.\n"
    "Everything in here may be created, changed, or deleted by those turns.\n"
)


def setup_console() -> None:
    # Piped stdout on Windows defaults to the ANSI code page; model text can contain any character.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)


def prepare_sandbox(remove: tuple[str, ...] = ()) -> None:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    readme = SANDBOX / "README.txt"
    if not readme.exists():
        readme.write_text(SANDBOX_README, encoding="utf-8")
    for name in remove:
        target = (SANDBOX / name).resolve()
        if target.parent == SANDBOX.resolve() and target.is_file():
            target.unlink()


def make_runtime() -> tuple[Runtime, dict[str, str]]:
    executable = find_droid()
    return Runtime(executable=executable), {
        "executable": str(executable),
        "version": droid_version(executable),
    }


def reasoning_effort(value: str | None = DEFAULT_REASONING) -> ReasoningEffort | None:
    return None if not value else ReasoningEffort(value)


def environment_info(droid: Mapping[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "sdk_version": droid_sdk.__version__,
        "droid_executable": droid["executable"],
        "droid_version": droid["version"],
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "factory_api_key_env_set": bool(os.environ.get("FACTORY_API_KEY")),
        **extra,
    }


# --------------------------------------------------------------------------- serializer

_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token"
    r"|bearer|token|secret|client[_-]?secret|password|passwd|authorization|cookie|credentials?)$",
    re.IGNORECASE,
)
_SECRET_VALUES = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bfk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
# Opaque provider blobs: large and useless for UI work.
_ELIDED_FIELDS = {
    "signature",
    "thought_signature",
    "thoughtSignature",
    "openai_encrypted_content",
    "openaiEncryptedContent",
}
_ELIDED_CLASS_FIELDS = {("RedactedThinkingBlock", "data"), ("Base64ImageSource", "data")}


def redact_text(value: str) -> str:
    for pattern in _SECRET_VALUES:
        value = pattern.sub("[REDACTED]", value)
    return value


def _field_value(owner: str, name: str, value: Any) -> Any:
    if _SECRET_KEY.match(name) and value not in (None, "", False):
        return "[REDACTED]"
    if isinstance(value, str) and value and (
        name in _ELIDED_FIELDS or (owner, name) in _ELIDED_CLASS_FIELDS
    ):
        return f"<elided {len(value)} chars>"
    return to_jsonable(value)


def to_jsonable(obj: Any) -> Any:
    """Convert SDK values (dataclasses, pydantic models, enums, mappings, ...) to JSON."""
    if obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<bytes len={len(obj)}>"
    owner = type(obj).__name__
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {"_type": owner}
        for item in dataclasses.fields(obj):
            out[item.name] = _field_value(owner, item.name, getattr(obj, item.name))
        return out
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump) and hasattr(type(obj), "model_fields"):
        dumped = model_dump(mode="json")
        out = {"_type": owner}
        out.update({str(k): _field_value(owner, str(k), v) for k, v in dumped.items()})
        return out
    if isinstance(obj, BaseException):
        return {"_type": owner, "message": redact_text(str(obj))}
    if owner == "RedactedStrMapping":
        return {str(k): "[REDACTED]" for k in obj}
    if isinstance(obj, Mapping):
        return {str(k): _field_value(owner, str(k), v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, Set)):
        return [to_jsonable(v) for v in obj]
    return {"_type": owner, "repr": redact_text(repr(obj))}


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False)


# --------------------------------------------------------------------------- timeline


def short_id(value: str | None) -> str:
    if not value:
        return "-"
    return value if len(value) <= 12 else f"{value[:8]}..{value[-3:]}"


def preview(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else dumps(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _blocks(message: Any) -> str:
    parts = []
    for block in message.content:
        kind = type(block).__name__.replace("Block", "")
        name = getattr(block, "name", None)
        parts.append(f"{kind}({name})" if name else kind)
    return "[" + ",".join(parts) + "]"


def describe(event: Any) -> str:
    name = type(event).__name__
    if isinstance(event, ToolCall):
        return f"ToolCall {event.name} id={short_id(event.tool_use_id)} input={preview(event.input)}"
    if isinstance(event, ToolResult):
        return (
            f"ToolResult {event.tool_name or '?'} id={short_id(event.tool_use_id)} "
            f"is_error={event.is_error} content={preview(event.content)}"
        )
    if isinstance(event, ToolProgress):
        update = event.update
        return (
            f"ToolProgress {event.tool_name} id={short_id(event.tool_use_id)} type={update.type} "
            f"status={update.status} content={preview(event.content, 60)}"
        )
    if isinstance(event, WorkingStateChanged):
        return f"WorkingStateChanged -> {event.state.value}"
    if isinstance(event, TokenUsageUpdate):
        return (
            f"TokenUsageUpdate in={event.input_tokens} out={event.output_tokens} "
            f"cache_read={event.cache_read_tokens} cache_write={event.cache_creation_tokens} "
            f"thinking={event.thinking_tokens} credits={event.factory_credits}"
        )
    if isinstance(event, PermissionResolved):
        ids = ",".join(short_id(i) for i in event.tool_use_ids)
        return f"PermissionResolved {event.selected_option.value} tool_use_ids=[{ids}]"
    if isinstance(event, (TextComplete, ThinkingComplete)):
        extra = f" duration={event.duration}" if isinstance(event, ThinkingComplete) else ""
        return f"{name} msg={short_id(event.message_id)} block={event.block_index}{extra}"
    if isinstance(event, (AssistantMessage, UserMessage)):
        return f"{name} id={short_id(event.id)} blocks={_blocks(event)} text={preview(event.text, 70)}"
    if isinstance(event, RunResult):
        usage = event.usage
        usage_text = (
            f" in={usage.input_tokens} out={usage.output_tokens} credits={usage.factory_credits}"
            if usage
            else ""
        )
        error = f" error={preview(event.error.message)}" if event.error else ""
        return (
            f"{name} subtype={event.subtype} duration={event.duration.total_seconds():.2f}s"
            f"{usage_text}{error} text={preview(event.text, 70)}"
        )
    if isinstance(event, ErrorEvent):
        return f"ErrorEvent {event.error_type.value}: {preview(event.message, 150)}"
    return f"{name} {preview(event)}"


class Timeline:
    """Compact console view; consecutive deltas of one block fold into one line."""

    def __init__(self, *, echo: bool = True) -> None:
        self.echo = echo
        self.lines: list[str] = []
        self._group: list[Any] | None = None

    def _emit(self, line: str) -> None:
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)

    def add(self, t: float, event: Any) -> None:
        if isinstance(event, (TextDelta, ThinkingDelta)):
            key = (type(event).__name__, event.message_id, event.block_index)
            if self._group is not None and self._group[0] == key:
                self._group[2] += 1
                self._group[3] += event.text
                return
            self.flush()
            self._group = [key, t, 1, event.text]
            return
        if isinstance(event, ToolCallDelta):
            key = ("ToolCallDelta", event.tool_use.id)
            if self._group is not None and self._group[0] == key:
                self._group[2] += 1
                self._group[3] = event
                return
            self.flush()
            self._group = [key, t, 1, event]
            return
        self.flush()
        self._emit(f"{t:8.3f}s  {describe(event)}")

    def note(self, t: float, text: str) -> None:
        self.flush()
        self._emit(f"{t:8.3f}s  {text}")

    def flush(self) -> None:
        group, self._group = self._group, None
        if group is None:
            return
        key, t, count, payload = group
        if key[0] == "ToolCallDelta":
            tool_use = payload.tool_use
            self._emit(
                f"{t:8.3f}s  ToolCallDelta x{count} {tool_use.name} id={short_id(tool_use.id)} "
                f"last_input={preview(tool_use.input)}"
            )
        else:
            self._emit(
                f"{t:8.3f}s  {key[0]} x{count} msg={short_id(key[1])} block={key[2]} "
                f"{len(payload)} chars: {preview(payload, 70)}"
            )


# --------------------------------------------------------------------------- recorder


class Recorder:
    """Writes every stream event, handler call and meta record to a JSONL fixture.

    Record shape: {"seq", "t" (s since recorder start), "turn", "turn_t" (s since
    turn start), "source" (stream|handler|meta), "type" (class name), "data"}.
    Raw wire notifications (session.on_notification) go to a separate file.
    """

    def __init__(self, path: Path, *, raw_path: Path | None = None, echo: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.raw_path = raw_path
        self._out = path.open("w", encoding="utf-8", newline="\n")
        self._raw = raw_path.open("w", encoding="utf-8", newline="\n") if raw_path else None
        self._seq = 0
        self._raw_seq = 0
        self._t0 = time.perf_counter()
        self._turn_t0 = self._t0
        self.turn = 0
        self.timeline = Timeline(echo=echo)
        self.latency: dict[int, dict[str, float]] = {}
        self.counts: dict[int, dict[str, int]] = {}
        self.order: dict[int, list[str]] = {}
        self.unsubscribe_raw: Callable[[], None] | None = None

    def now(self) -> float:
        return time.perf_counter() - self._t0

    def turn_now(self) -> float:
        return time.perf_counter() - self._turn_t0

    def mark(self, name: str) -> None:
        self.latency.setdefault(self.turn, {}).setdefault(name, round(self.turn_now(), 4))

    def write(self, source: str, type_name: str, payload: Any) -> None:
        data = to_jsonable(payload)
        if isinstance(data, dict):
            data.pop("_type", None)
        record = {
            "seq": self._seq,
            "t": round(self.now(), 4),
            "turn": self.turn,
            "turn_t": round(self.turn_now(), 4) if self.turn else None,
            "source": source,
            "type": type_name,
            "data": data,
        }
        self._out.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._out.flush()
        self._seq += 1

    def start_turn(self, prompt: str, **extra: Any) -> None:
        self.turn += 1
        self._turn_t0 = time.perf_counter()
        self.write("meta", "TurnStarted", {"prompt": prompt, **extra})
        self.timeline.note(0.0, f"===== turn {self.turn}: {preview(prompt, 100)}")

    def event(self, event: Any) -> None:
        name = type(event).__name__
        self.write("stream", name, event)
        counts = self.counts.setdefault(self.turn, {})
        counts[name] = counts.get(name, 0) + 1
        order = self.order.setdefault(self.turn, [])
        if not order or order[-1] != name:
            order.append(name)
        self.mark("first_event")
        self.mark(f"first_{name}")
        if isinstance(event, (ToolCallDelta, ToolCall, ToolProgress, ToolResult)):
            self.mark("first_tool_event")
        self.timeline.add(self.turn_now(), event)

    def raw(self, notification: Mapping[str, object]) -> None:
        if self._raw is None:
            return
        record = {
            "seq": self._raw_seq,
            "t": round(self.now(), 4),
            "turn": self.turn,
            "notification": to_jsonable(notification),
        }
        self._raw.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._raw.flush()
        self._raw_seq += 1

    def meta(self, type_name: str, **data: Any) -> None:
        self.write("meta", type_name, data)

    def handler(self, type_name: str, payload: Any, summary: str) -> None:
        self.mark(f"first_{type_name}")
        self.write("handler", type_name, payload)
        self.timeline.note(self.turn_now(), summary)

    def summary(self) -> dict[str, Any]:
        return {
            "latency_s": {str(k): v for k, v in self.latency.items()},
            "counts": {str(k): v for k, v in self.counts.items()},
            "order": {str(k): v for k, v in self.order.items()},
        }

    def print_summary(self) -> None:
        for turn in sorted(self.order):
            print(f"--- turn {turn} event order (consecutive duplicates folded):")
            print("    " + " > ".join(self.order[turn]))
            print(f"--- turn {turn} counts: {json.dumps(self.counts.get(turn, {}))}")
            print(f"--- turn {turn} first-occurrence latency (s): {json.dumps(self.latency.get(turn, {}))}")

    def close(self) -> None:
        self.timeline.flush()
        self.meta("Summary", **self.summary())
        self._out.close()
        if self._raw is not None:
            self._raw.close()


async def consume(stream: Any, recorder: Recorder, on_event: Callable[[Any], Any] | None = None) -> Any:
    async for event in stream:
        recorder.event(event)
        if on_event is not None:
            outcome = on_event(event)
            if hasattr(outcome, "__await__"):
                await outcome
    return stream.result


async def open_recorded(session: Any, recorder: Recorder, *, raw: bool = True) -> None:
    """Open the session, record SessionOpened, and mirror raw wire notifications."""
    started = time.perf_counter()
    await session.open()
    open_s = time.perf_counter() - started
    recorder.meta(
        "SessionOpened",
        session_id=session.id,
        cwd=session.cwd,
        settings=session.settings,
        open_seconds=round(open_s, 3),
    )
    print(
        f"session {session.id} opened in {open_s:.2f}s cwd={session.cwd} "
        f"model={session.settings.model} reasoning={session.settings.reasoning_effort} "
        f"autonomy={session.settings.autonomy}",
        flush=True,
    )
    if raw:
        recorder.unsubscribe_raw = session.on_notification(recorder.raw)


async def run_turn(
    session: Any,
    recorder: Recorder,
    prompt: str,
    *,
    timeout: float,
    on_event: Callable[[Any], Any] | None = None,
) -> Any:
    recorder.start_turn(prompt)
    async with session.stream(prompt, include_partial_messages=True, timeout=timeout) as stream:
        return await consume(stream, recorder, on_event)


async def close_recorded(session: Any, recorder: Recorder) -> None:
    unsubscribe = getattr(recorder, "unsubscribe_raw", None)
    if unsubscribe is not None:
        unsubscribe()
        recorder.unsubscribe_raw = None
    started = time.perf_counter()
    await session.close()
    recorder.meta("SessionClosed", close_seconds=round(time.perf_counter() - started, 3))


# --------------------------------------------------------------------------- permission policy

_WIN_ABS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Z]:[\\/][^\s'\"|;&<>`,)]*)")
_PARENT_REF = re.compile(r"(?:^|[\s'\"\\/=])\.\.(?:$|[\s'\"\\/])")
_INDIRECT_REF = re.compile(r"(?i)\$env:|\$home\b|%[A-Z_]+%|(?:^|\s)~[\\/]|\\\\[A-Za-z0-9]")


def inside_sandbox(path_text: str) -> bool:
    try:
        path = Path(path_text)
        if not path.is_absolute():
            path = SANDBOX / path
        path.resolve(strict=False).relative_to(SANDBOX.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def review_action(action: Any) -> tuple[bool, str]:
    """Heuristic guard that keeps spike turns inside the sandbox folder."""
    if isinstance(action, (CreateFile, EditAction)):
        ok = inside_sandbox(action.file_path)
        return ok, "inside sandbox" if ok else f"outside sandbox: {action.file_path}"
    if isinstance(action, ApplyPatchAction):
        paths = [action.file_path]
        for item in action.files or ():
            paths.append(item.file_path)
            if item.move_to:
                paths.append(item.move_to)
        bad = [p for p in paths if p and not inside_sandbox(p)]
        return not bad, "inside sandbox" if not bad else f"outside sandbox: {bad}"
    if isinstance(action, ExecuteAction):
        command = action.full_command or action.command
        if _PARENT_REF.search(command):
            return False, "parent-directory reference in command"
        if _INDIRECT_REF.search(command):
            return False, "env/home/UNC reference in command"
        bad = [m.group(1) for m in _WIN_ABS_PATH.finditer(command) if not inside_sandbox(m.group(1))]
        if bad:
            return False, f"absolute path outside sandbox: {bad}"
        return True, "no outside paths"
    if isinstance(action, AskUserAction):
        return True, "AskUser touches no files"
    return False, f"{type(action).__name__} is not allowed in spikes"


def _action_brief(action: Any) -> str:
    if isinstance(action, ExecuteAction):
        return f"{preview(action.full_command or action.command, 70)} impact={action.impact_level}"
    if isinstance(action, (CreateFile, EditAction, ApplyPatchAction)):
        return action.file_path
    if isinstance(action, AskUserAction):
        return f"{len(action.questions)} question(s)"
    return type(action).__name__


def make_permission_handler(recorder: Recorder) -> Callable[[PermissionRequest], PermissionResponse]:
    def on_permission(request: PermissionRequest) -> PermissionResponse:
        offered = [option.value for option in request.options]
        reviews = [review_action(action) for action in request.actions]
        allowed = bool(request.actions) and all(ok for ok, _ in reviews)
        if allowed and ToolConfirmationOutcome.PROCEED_ONCE in offered:
            response = request.respond(ToolConfirmationOutcome.PROCEED_ONCE)
        elif ToolConfirmationOutcome.CANCEL in offered:
            response = request.respond(ToolConfirmationOutcome.CANCEL)
        else:
            # An outcome Droid did not offer is treated as a cancellation by the SDK.
            response = PermissionResponse(selected_option=ToolConfirmationOutcome.CANCEL)
        actions = "; ".join(f"{type(a).__name__}({_action_brief(a)})" for a in request.actions)
        recorder.handler(
            "PermissionRequest",
            {
                "request": request,
                "policy": [{"allowed": ok, "reason": why} for ok, why in reviews],
                "response": response,
            },
            f"[permission] {actions} options={[o.value for o in offered]} -> {response.selected_option.value}",
        )
        return response

    return on_permission


def first_option(question: Any) -> str:
    return question.options[0] if question.options else "none"


def make_question_handler(
    recorder: Recorder, choose: Callable[[Any], str] = first_option
) -> Callable[[QuestionRequest], QuestionResponse]:
    def on_question(request: QuestionRequest) -> QuestionResponse:
        response = request.submit([question.answer(choose(question)) for question in request.questions])
        asked = " | ".join(f"{q.question} {list(q.options)}" for q in request.questions)
        answered = [a.answer for a in response.answers]
        recorder.handler(
            "QuestionRequest",
            {"request": request, "response": response},
            f"[question] {preview(asked, 160)} -> {answered}",
        )
        return response

    return on_question
