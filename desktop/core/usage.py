"""Token use of a whole conversation, however many times it was disconnected, closed, reopened or moved to another engine.

Each engine counts per native session (an own-loop session, a Devin session) and keeps counting across a resume: what it
reports is that session's running total. A conversation is more than one session when its engine was changed, and the
page wants the sum, so the app keeps a ledger: the total so far, and the last figure each running series reported
(``seen``). A new figure adds only what is new since the last one of its series; a series that starts over (a new
session after an engine change, a sub-agent) is counted in full. Both halves are written to the cache, so after a
restart the total goes on from where it was instead of starting again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Any


def _count(value: Any) -> int | None:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True, slots=True)
class Tokens:
    """Token counts in one shape for every engine (what ``viewmodel.usage_json`` reads).

    ``input_tokens`` is what was not served from the cache; ``cache_creation_tokens`` is ``None`` where the engine has
    no such figure (the Responses API)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int | None = None
    cache_read_tokens: int = 0

    @classmethod
    def of(cls, usage: Any) -> Tokens | None:
        """From an engine's usage object (attributes ``input_tokens`` …), a page / cache JSON (``input``, ``output``,
        ``cacheWrite``, ``cacheRead``) or an engine's notification (``inputTokens``…)."""
        if usage is None:
            return None
        if isinstance(usage, Tokens):
            return usage
        if isinstance(usage, Mapping):
            get = usage.get
            pick = lambda *names: next((_count(get(n)) for n in names if _count(get(n)) is not None), None)  # noqa: E731
            fields = (
                pick("input", "inputTokens", "input_tokens"),
                pick("output", "outputTokens", "output_tokens"),
                pick("cacheWrite", "cacheCreationTokens", "cache_creation_tokens"),
                pick("cacheRead", "cacheReadTokens", "cache_read_tokens"),
            )
        else:
            fields = tuple(_count(getattr(usage, name, None)) for name in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"))
        if all(value is None for value in fields):
            return None
        i, o, w, r = fields
        return cls(i or 0, o or 0, w, r or 0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + (self.cache_creation_tokens or 0) + self.cache_read_tokens

    def peak(self, other: Tokens | None) -> Tokens:
        """Field by field the larger: one session's counters only grow, so the larger is the newer."""
        if other is None:
            return self
        write = None if self.cache_creation_tokens is None and other.cache_creation_tokens is None else max(self.cache_creation_tokens or 0, other.cache_creation_tokens or 0)
        return Tokens(
            max(self.input_tokens, other.input_tokens),
            max(self.output_tokens, other.output_tokens),
            write,
            max(self.cache_read_tokens, other.cache_read_tokens),
        )

    def plus(self, other: Tokens) -> Tokens:
        write = None if self.cache_creation_tokens is None and other.cache_creation_tokens is None else (self.cache_creation_tokens or 0) + (other.cache_creation_tokens or 0)
        return Tokens(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            write,
            self.cache_read_tokens + other.cache_read_tokens,
        )

    def since(self, earlier: Tokens) -> Tokens | None:
        """What was added after ``earlier``; ``None`` when a counter is lower than it was (the series started over)."""
        write = None if self.cache_creation_tokens is None and earlier.cache_creation_tokens is None else (self.cache_creation_tokens or 0) - (earlier.cache_creation_tokens or 0)
        parts = (
            self.input_tokens - earlier.input_tokens,
            self.output_tokens - earlier.output_tokens,
            self.cache_read_tokens - earlier.cache_read_tokens,
        )
        if min(parts) < 0 or (write is not None and write < 0):
            return None
        return Tokens(parts[0], parts[1], write, parts[2])

    def to_json(self) -> dict[str, Any]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cacheWrite": self.cache_creation_tokens,
            "cacheRead": self.cache_read_tokens,
        }


def own_output(series: Mapping[str, Tokens]) -> int | None:
    """Output tokens the session itself has counted so far (sub-agents' threads, ``agent:...``, run beside it and are not
    its own); ``None`` while it has reported nothing."""
    own = [tokens.output_tokens for key, tokens in series.items() if not key.startswith("agent:")]
    return sum(own) if own else None


@dataclass(slots=True)
class UsageLedger:
    """The conversation's total, and what each series that feeds it last said."""

    total: Tokens | None = None
    seen: dict[str, Tokens] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    model_seen: dict[str, Tokens] = field(default_factory=dict)
    """Known model attribution has its own series: legacy totals may have no attribution at all."""

    def feed(self, series: Mapping[str, Tokens]) -> bool:
        """Take the figures the engine reports now; ``True`` when the total changed."""
        before = self.total
        for key, now in series.items():
            earlier = self.seen.get(key)
            added = now if earlier is None else (now.since(earlier) or now)
            self.total = added if self.total is None else self.total.plus(added)
            self.seen[key] = now
        return self.total != before

    def feed_models(self, series: Mapping[str, tuple[str, Tokens]]) -> None:
        """Attribute known usage without changing the authoritative total or guessing a legacy session's model."""
        for key, (model, now) in series.items():
            if not model:
                continue
            earlier = self.model_seen.get(key)
            added = now if earlier is None else (now.since(earlier) or now)
            self.by_model[model] = self.by_model.get(model, 0) + added.total
            self.model_seen[key] = now

    def restart(self) -> None:
        """The next figures belong to new sessions (the engine was changed): they are counted in full."""
        self.seen.clear()
        self.model_seen.clear()

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total.to_json() if self.total else None,
            "seen": {key: value.to_json() for key, value in self.seen.items()},
            "byModel": dict(self.by_model),
            "modelSeen": {key: value.to_json() for key, value in self.model_seen.items()},
        }

    @classmethod
    def from_json(cls, raw: Any) -> UsageLedger:
        if not isinstance(raw, Mapping):
            return cls()
        seen_raw = raw.get("seen")
        seen = {}
        if isinstance(seen_raw, Mapping):
            for key, value in seen_raw.items():
                tokens = Tokens.of(value)
                if isinstance(key, str) and tokens is not None:
                    seen[key] = tokens
        models = raw.get("byModel")
        by_model = {key: int(value) for key, value in models.items() if isinstance(key, str) and _count(value) is not None} if isinstance(models, Mapping) else {}
        model_raw = raw.get("modelSeen")
        model_seen = {}
        if isinstance(model_raw, Mapping):
            for key, value in model_raw.items():
                tokens = Tokens.of(value)
                if isinstance(key, str) and tokens is not None:
                    model_seen[key] = tokens
        return cls(Tokens.of(raw.get("total")), seen, by_model, model_seen)
