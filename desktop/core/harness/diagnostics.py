"""Bounded, content-free request diagnostics. Never persist response ids, text, URLs or error bodies."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

from desktop.core.chatgpt.responses import Finished

MAX_RECORDS = 128
_REASONS = frozenset({
    "tools_changed", "input_changed", "instructions_changed", "reasoning_effort_changed", "context_compacted",
    "model_changed", "prompt_cache_key_changed", "prefix_mismatch", "cache_expired", "cache_not_found",
    "no_matching_cache", "below_minimum_length", "cache_hit", "cache_miss",
})


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reasons(value: Any) -> list[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, part in value.items():
            if key in ("reason", "code", "reason_code") and isinstance(part, str) and part in _REASONS:
                found.add(part)
            elif key in ("reasons", "miss_reasons", "reason_codes"):
                found.update(_reasons(part))
    elif isinstance(value, list):
        for part in value:
            if isinstance(part, str) and part in _REASONS:
                found.add(part)
            else:
                found.update(_reasons(part))
    return sorted(found)


class RequestTrace:
    def __init__(self) -> None:
        self._fixed: str = ""
        self._items: list[str] = []

    def record(self, request: Mapping[str, Any], finished: Finished | None, seconds: float, failed: bool = False) -> dict[str, Any]:
        fixed = fingerprint([request.get(key) for key in ("model", "instructions", "tools", "reasoning", "wc_reasoning_effort")])
        items = [fingerprint(item) for item in request.get("input") or ()]
        shared = 0
        if self._fixed == fixed:
            for before, after in zip(self._items, items):
                if before != after:
                    break
                shared += 1
        self._fixed, self._items = fixed, items
        record: dict[str, Any] = {
            "at": round(time.time(), 3), "seconds": round(max(seconds, 0), 3), "failed": failed,
            "model_hash": fingerprint(request.get("model")), "instructions_hash": fingerprint(request.get("instructions")),
            "tools_hash": fingerprint(request.get("tools")), "reasoning_hash": fingerprint([request.get("reasoning"), request.get("wc_reasoning_effort")]),
            "input_hash": fingerprint(items), "input_items": len(items), "shared_prefix_items": shared,
        }
        if finished is not None:
            if finished.usage is not None:
                record.update(finished.usage.tokens().to_json())
            diagnostics = finished.diagnostics
            reasoning = diagnostics.get("reasoning")
            if isinstance(reasoning, Mapping) and reasoning.get("context") in ("all_turns", "current_turn"):
                record["reasoning_context"] = reasoning["context"]
            cache = diagnostics.get("prompt_cache_diagnostics")
            if isinstance(cache, Mapping):
                status = cache.get("type", cache.get("status"))
                if status in ("cache_hit", "cache_miss", "comparison_response_not_found", "unavailable"):
                    record["cache_status"] = status
                record["cache_reasons"] = _reasons(cache)
                for key in ("comparison_reusable_tokens", "cache_missed_tokens"):
                    value = cache.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        record[key] = value
            transformations = diagnostics.get("input_transformations")
            if isinstance(transformations, list):
                record["input_transformations"] = len(transformations)
                record["transformation_reasons"] = _reasons(transformations)
        return record
