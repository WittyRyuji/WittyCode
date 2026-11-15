"""What a task took, read from the conversation the loop left behind (nothing here asks a model anything).

The figures come from what the application itself keeps: the request diagnostics of the conversation
(``harness.diagnostics``: one record per request with its tokens, its time and how much of the request before it was
its beginning), the token total of the conversation, and the turn of the transcript (when the tools ran).

Tokens are counted the way the application shows them: input that was not cached, cache writes, cache reads and output
are four separate figures, and their sum is what a budget is measured in. ``Price`` turns them into one figure for
comparing two runs. It is a ratio, not a bill: what a subscription charges for cached tokens is not published.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

KINDS = ("input", "cacheWrite", "cacheRead", "output")
ENDS = {
    "done": "the model ended its turn",
    "limit": "the step limit was reached",
    "incomplete": "the answer was cut short or empty",
    "failed": "the turn failed (a request was refused, or the harness raised)",
    "interrupted": "the turn was stopped",
    "budget": "stopped by the evaluation: the task's tokens were used up",
    "timeout": "stopped by the evaluation: the task's time was used up",
    "unmetered": "stopped by the evaluation: the service reported no token usage, so no budget could be kept",
    "skipped": "not run: the run's token budget was used up before it",
    "error": "the evaluation itself failed on this task",
}
"""How a task's turn can end (``TaskResult.end``)."""
_OUTPUT_SHOWN = 600
_ARGS_SHOWN = 400


@dataclass(frozen=True)
class Price:
    """What a token of each kind costs, in uncached input tokens. The default is the ratio of the list prices of OpenAI's
    GPT-6 models (cache read 0.1, cache write 1.25, output 5); give another with ``--price`` for a model priced otherwise."""

    read: float = 0.1
    write: float = 1.25
    output: float = 5.0

    def units(self, tokens: Mapping[str, int]) -> float:
        return round(tokens["input"] + tokens["cacheWrite"] * self.write + tokens["cacheRead"] * self.read + tokens["output"] * self.output, 1)


def no_tokens() -> dict[str, int]:
    return {"input": 0, "cacheWrite": 0, "cacheRead": 0, "output": 0, "total": 0}


def no_seconds() -> dict[str, float]:
    return {"turn": 0.0, "requests": 0.0, "tools": 0.0, "other": 0.0, "first_output": 0.0, "check": 0.0}


@dataclass
class TaskResult:
    id: str
    title: str = ""
    passed: bool = False
    """The task's check passes on what the agent left, however the turn ended."""
    end: str = "skipped"
    """One of ``ENDS``."""
    note: str = ""
    """What the harness said about a turn that did not simply end (its error, its notice)."""
    check: str = ""
    """What the check said when it failed."""
    requests: int = 0
    """Requests to the model for the conversation itself (the steps of the turn)."""
    failed_requests: int = 0
    summaries: int = 0
    """Requests that wrote a summary of the conversation (a compaction)."""
    prefix_breaks: int = 0
    """Requests (after the first) that did not begin with the whole request before them: what a prompt cache cannot reuse."""
    tool_calls: int = 0
    tool_errors: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    approvals: int = 0
    """Times a user would have been asked to allow something (the evaluation allowed it)."""
    refusals: int = 0
    """Times the loop asked to read or change a file outside the task's folder (the evaluation refused)."""
    questions: int = 0
    """Questions the model put to the user (the evaluation left them unanswered)."""
    compacted: bool = False
    tokens: dict[str, int] = field(default_factory=no_tokens)
    peak_context: int = 0
    """The input of the largest request."""
    cost_units: float = 0.0
    seconds: dict[str, float] = field(default_factory=no_seconds)
    """``turn``: start to end; ``requests``: waiting for and reading the model's answers; ``tools``: tools at work;
    ``other``: the rest of the turn (pauses before retries, summaries, the harness's own work); ``first_output``: until the
    model first wrote anything; ``check``: the task's check, after the turn."""
    changed: list[str] = field(default_factory=list)
    """Files of the project that are not as the task gave them."""

    @property
    def hit_rate(self) -> float | None:
        read, base = self.tokens["cacheRead"], self.tokens["input"] + self.tokens["cacheWrite"] + self.tokens["cacheRead"]
        return read / base if base else None

    @property
    def ran(self) -> bool:
        """Whether the task was given to the loop at all (one the evaluation failed on counts: it was not solved)."""
        return self.end != "skipped"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> TaskResult:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def tokens_of(usage: Any) -> dict[str, int]:
    """The four figures and their sum, from the conversation's total (``core.usage.Tokens``), a request's record or ``None``."""
    if usage is None:
        return no_tokens()
    if isinstance(usage, Mapping):
        parts = {kind: _number(usage.get(kind)) for kind in KINDS}
    else:
        parts = {
            "input": _number(usage.input_tokens),
            "cacheWrite": _number(usage.cache_creation_tokens),
            "cacheRead": _number(usage.cache_read_tokens),
            "output": _number(usage.output_tokens),
        }
    return {**parts, "total": sum(parts.values())}


def request_input(record: Mapping[str, Any]) -> int:
    """All the input of one request, cached or not."""
    return _number(record.get("input")) + _number(record.get("cacheWrite")) + _number(record.get("cacheRead"))


def request_total(record: Mapping[str, Any]) -> int:
    return request_input(record) + _number(record.get("output"))


def prefix_breaks(records: Sequence[Mapping[str, Any]]) -> int:
    """How many requests did not begin with all of the request before them (``shared_prefix_items`` is counted against the
    request sent just before, whatever it was for). A request sent again after a failure is the same request: no break."""
    breaks = 0
    for before, record in zip(records, records[1:]):
        if _number(record.get("shared_prefix_items")) < _number(before.get("input_items")):
            breaks += 1
    return breaks


def _covered(spans: Sequence[tuple[float, float]]) -> float:
    """The time covered by the spans together (tools that ran at the same time count once)."""
    total, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def _saved_end(controller: Any, turn: Any) -> tuple[str, str]:
    """How the turn ended and what the harness said about it."""
    record = controller.saved.turns[-1] if controller.saved.turns else None
    if record is not None and record.finished is not None:
        end = record.status if record.status in ("done", "limit", "incomplete", "failed", "interrupted") else "done"
        return end, (record.error or record.note or "").strip()
    # (a turn of which nothing came is not kept in the saved conversation: the page's turn says what happened)
    status = getattr(getattr(turn, "status", None), "value", "")
    errors = [block.message for block in getattr(turn, "blocks", ()) if getattr(block, "kind", "") == "error"]
    return {"failed": "failed", "interrupted": "interrupted", "done": "done"}.get(status, "failed"), (errors[-1] if errors else "").strip()


def measure(controller: Any, price: Price = Price()) -> dict[str, Any]:
    """The fields of a ``TaskResult`` that the conversation of a finished turn gives."""
    records = [record for record in controller.saved.requests if isinstance(record, Mapping)]
    steps = [record for record in records if record.get("kind") != "summary"]
    tokens = tokens_of(controller.usage)
    turn = controller.transcript.turns[-1] if controller.transcript.turns else None
    end, note = _saved_end(controller, turn)
    items = list(turn.tools.values()) if turn is not None else []
    names: dict[str, int] = {}
    for item in items:
        names[item.name] = names.get(item.name, 0) + 1
    spans = [(item.started_at, item.finished_at) for item in items if item.started_at is not None and item.finished_at is not None]
    whole = float(turn.duration or 0.0) if turn is not None else 0.0
    asked = sum(float(record.get("seconds") or 0.0) for record in records)
    worked = _covered(spans)
    first = turn.first_output_at - turn.started_at if turn is not None and turn.first_output_at is not None and turn.started_at is not None else 0.0
    seconds = no_seconds()
    seconds.update(turn=round(whole, 2), requests=round(asked, 2), tools=round(worked, 2), other=round(max(whole - asked - worked, 0.0), 2), first_output=round(max(first, 0.0), 2))
    return {
        "end": end,
        "note": note,
        "requests": len(steps),
        "failed_requests": sum(1 for record in records if record.get("failed")),
        "summaries": len(records) - len(steps),
        "prefix_breaks": prefix_breaks(records),
        "tool_calls": len(items),
        "tool_errors": sum(1 for item in items if item.is_error),
        "tools": dict(sorted(names.items())),
        "compacted": bool(controller.saved.compaction),
        "tokens": tokens,
        "peak_context": max((request_input(record) for record in steps), default=0),
        "cost_units": price.units(tokens),
        "seconds": seconds,
    }


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[: limit * 2 // 3]}\n[... {len(text) - limit} characters ...]\n{text[-(limit // 3):]}"


def _said(item: Mapping[str, Any]) -> str:
    return "".join(str(part.get("text") or "") for part in item.get("content") or () if isinstance(part, Mapping)).strip()


def transcript_text(items: Sequence[Mapping[str, Any]]) -> str:
    """The conversation as text for a person who wants to see why a task went as it did: what was said in full, the calls
    and what came back cut short. The whole of it is in the saved conversation."""
    out: list[str] = []
    for item in items:
        kind = item.get("type")
        if kind == "message":
            text = _said(item)
            if text:
                out.append(f"## {item.get('role', 'assistant')}\n\n{text}")
        elif kind == "function_call":
            out.append(f"### call {item.get('name')}\n\n```\n{_clip(str(item.get('arguments') or ''), _ARGS_SHOWN)}\n```")
        elif kind == "function_call_output":
            out.append(f"```\n{_clip(str(item.get('output') or ''), _OUTPUT_SHOWN)}\n```")
    return "\n\n".join(out) + "\n"
