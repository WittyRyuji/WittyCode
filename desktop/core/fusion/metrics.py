"""Task-local Fusion counts (design 10 and 13.4), without persistence or tuning."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

RETAINED_TASKS = 200
_DESTINATIONS = ("", "self", "coder", "split")
_SCORES = ("-1", "0", "1")


@dataclass(slots=True)
class Usage:
    """Sum each token field across runs; ``unknown`` counts missing totals."""

    runs: int = 0
    tokens: int | None = 0
    seconds: float = 0.0
    first_request_tokens: int | None = None
    unknown: int = 0


@dataclass(slots=True)
class TaskMetrics:
    destination: str = ""
    experts: dict[str, Usage] = field(default_factory=dict)
    main: Usage = field(default_factory=Usage)
    runner_first_pass: bool | None = None
    sampler_blocking: bool | None = None
    misroutes: int = 0
    observer_reports: int = 0
    observer_wrong: int = 0
    compactions: int = 0
    drive: dict[str, int] = field(default_factory=dict)
    accepted: bool | None = None


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _seconds(value: Any) -> bool:
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("Expected a boolean result.")
    return value


def _drive_counts(counts: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(counts, Mapping) or any(type(key) is not str or not _count(value)
                                              for key, value in counts.items()):
        raise ValueError("Drive counters must map names to nonnegative integers.")
    return dict(counts)


def _total_tokens(left: int | None, right: int | None) -> int | None:
    return None if left is None or right is None else left + right


def _add_run(usage: Usage, tokens: int | None, seconds: float, first: int | None = None) -> None:
    if ((tokens is not None and not _count(tokens)) or (first is not None and not _count(first))
            or not _seconds(seconds) or not _seconds(usage.seconds + seconds)):
        raise ValueError("Usage requires nonnegative token counts or None and finite elapsed seconds.")
    usage.first_request_tokens = _total_tokens(usage.first_request_tokens, first) if usage.runs else first
    usage.runs += 1
    usage.tokens = _total_tokens(usage.tokens, tokens)
    usage.seconds += seconds
    usage.unknown += tokens is None


def drive_counts(before: Mapping, after: Mapping) -> dict[str, int]:
    """Project counters are cumulative; remove neutral defaults from the explicit rating shares."""
    counts = {name: max(0, after[key] - before.get(key, 0)) for name, key in (
        ("shared_hits", "shared_hits"), ("unrated", "unrated"), ("out_of_use", "expired"), ("above_qk", "above_qk"),
    )}
    for score in (-1, 0, 1):
        difference = after["ratings"][score] - before.get("ratings", {}).get(score, 0)
        counts[f"rating_{score}"] = max(0, difference - (counts["unrated"] if score == 0 else 0))
    return counts


class Recorder:
    """Accumulate one user turn at a time; the caller persists the result."""

    def __init__(self) -> None:
        self._id = ""
        self._task: TaskMetrics | None = None

    def _current(self) -> TaskMetrics:
        if self._task is None:
            raise ValueError("Begin a metrics task first.")
        return self._task

    def begin(self, task_id: str) -> None:
        if self._task is not None:
            raise ValueError("Finish the current metrics task first.")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("A task ID is required.")
        self._id, self._task = task_id, TaskMetrics()

    def route(self, destination: str) -> None:
        task = self._current()
        if destination not in _DESTINATIONS:
            raise ValueError("Use an empty destination or self, coder or split.")
        task.destination = destination

    def expert(self, identity: str, *, tokens: int | None, seconds: float,
               first_request_tokens: int | None = None) -> None:
        task = self._current()
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("An expert identity is required.")
        usage = task.experts.get(identity, Usage())
        _add_run(usage, tokens, seconds, first_request_tokens)
        task.experts[identity] = usage

    def main(self, tokens: int | None, seconds: float) -> None:
        _add_run(self._current().main, tokens, seconds)

    def runner(self, passed: bool) -> None:
        task = self._current()
        passed = _boolean(passed)
        if task.runner_first_pass is None:
            task.runner_first_pass = passed

    def sampler(self, blocking: bool) -> None:
        task = self._current()
        blocking = _boolean(blocking)
        task.sampler_blocking = bool(task.sampler_blocking or blocking)

    def misroute(self) -> None:
        self._current().misroutes += 1

    def observer(self, wrong: bool = False) -> None:
        """Count one report; only an adjudicated judgement can be wrong."""
        task = self._current()
        wrong = _boolean(wrong)
        task.observer_reports += 1
        task.observer_wrong += wrong

    def compaction(self) -> None:
        self._current().compactions += 1

    def drive(self, counts: Mapping[str, int]) -> None:
        """Add task-local deltas, not project-wide snapshots.

        Use shared_hits, out_of_use, above_qk, rating_-1, rating_0, rating_1 and unrated.
        """
        task = self._current()
        for key, value in _drive_counts(counts).items():
            task.drive[key] = task.drive.get(key, 0) + value

    def accept(self, ok: bool) -> None:
        task = self._current()
        task.accepted = _boolean(ok)

    def finish(self) -> dict:
        """Close the task and return an independent JSON-ready snapshot."""
        raw = asdict(self._current())
        raw["task_id"] = self._id
        self._task, self._id = None, ""
        return raw


def _columns(raw: Any, schema: type) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("Expected a metrics object.")
    return {part.name: raw[part.name] for part in fields(schema)}


def _load_usage(raw: Any) -> Usage:
    usage = Usage(**_columns(raw, Usage))
    if (not _count(usage.runs) or not _count(usage.unknown) or usage.unknown > usage.runs
            or (usage.unknown > 0) != (usage.tokens is None)
            or (usage.tokens is not None and not _count(usage.tokens))
            or (usage.first_request_tokens is not None and not _count(usage.first_request_tokens))
            or not _seconds(usage.seconds)):
        raise ValueError("Invalid saved usage.")
    if not usage.runs and (usage.tokens or usage.seconds or usage.first_request_tokens is not None):
        raise ValueError("Usage totals require at least one run.")
    return usage


def load(raw: Any) -> TaskMetrics | None:
    """Read a finished snapshot; missing or invalid measurements are not zeros."""
    try:
        task = TaskMetrics(**_columns(raw, TaskMetrics))
        if task.destination not in _DESTINATIONS or not isinstance(task.experts, Mapping):
            return None
        if any(type(identity) is not str or not identity.strip() for identity in task.experts):
            return None
        if any(value is not None and type(value) is not bool
               for value in (task.runner_first_pass, task.sampler_blocking, task.accepted)):
            return None
        if not all(map(_count, (task.misroutes, task.observer_reports, task.observer_wrong, task.compactions))):
            return None
        if task.observer_wrong > task.observer_reports:
            return None
        task.experts = {identity: _load_usage(usage) for identity, usage in task.experts.items()}
        task.main = _load_usage(task.main)
        task.drive = _drive_counts(task.drive)
        return task
    except (KeyError, TypeError, ValueError):
        return None


def retained_tasks(raw: Any) -> list[dict]:
    """Ignore malformed observations without making a conversation impossible to open."""
    entries = []
    for item in raw[-RETAINED_TASKS:] if isinstance(raw, list) else ():
        if (task := load(item)) is not None:
            ident = item.get("task_id")
            if isinstance(ident, str) and 0 < len(ident) <= 128:
                entries.append({**asdict(task), "task_id": ident})
    return entries


def summary(tasks: Sequence[Mapping]) -> dict:
    """Cost of all valid tasks per accepted task, with small-task costs broken out.

    Failed attempts still cost time and tokens. First-pass rate uses tasks with
    Runner results; rating shares exclude unrated records.
    """
    destinations = dict.fromkeys(_DESTINATIONS, 0)
    costs = {destination: Usage() for destination in _DESTINATIONS}
    total = Usage()
    ratings = dict.fromkeys(_SCORES, 0)
    accepted = runner_seen = runner_passed = misroutes = wrong = expert_runs = unrated = 0
    for raw in tasks:
        task = load(raw)
        if task is None:
            continue
        destinations[task.destination] += 1
        accepted += task.accepted is True
        runner_seen += task.runner_first_pass is not None
        runner_passed += task.runner_first_pass is True
        misroutes += task.misroutes
        wrong += task.observer_wrong
        expert_runs += sum(usage.runs for usage in task.experts.values())
        unrated += task.drive.get("unrated", 0)
        for score in ratings:
            ratings[score] += task.drive.get(f"rating_{score}", 0)
        for usage in (task.main, *task.experts.values()):
            for cost in (total, costs[task.destination]):
                cost.tokens = _total_tokens(cost.tokens, usage.tokens)
                cost.seconds += usage.seconds
    rated = sum(ratings.values())
    return {
        "tasks": sum(destinations.values()),
        "accepted": accepted,
        "tokens_per_accepted": (None if total.tokens is None else total.tokens / accepted) if accepted else 0.0,
        "seconds_per_accepted": total.seconds / accepted if accepted else 0.0,
        "destinations": destinations,
        "unrouted": {"tokens": costs[""].tokens, "seconds": costs[""].seconds},
        "self": {"tokens": costs["self"].tokens, "seconds": costs["self"].seconds},
        "runner_first_pass_rate": runner_passed / runner_seen if runner_seen else 0.0,
        "misroutes": misroutes,
        "observer_false_report_rate": wrong / expert_runs if expert_runs else 0.0,
        "rating_shares": {score: count / rated if rated else 0.0 for score, count in ratings.items()},
        "unrated": unrated,
    }
