"""Bounded per-model Fusion effort overrides; engine-specific level names stay intact."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def reasoning_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 256 or any(
        not isinstance(model, str) or not model.startswith(("chatgpt:", "api:", "devin:")) or len(model) > 512
        or not isinstance(effort, str) or not effort.strip() or len(effort) > 64
        for model, effort in value.items()
    ):
        raise ValueError("The Fusion reasoning settings are invalid.")
    return dict(value)


def configured_effort(values: Mapping[str, str], model: str, efforts: Iterable[str]) -> str | None:
    """Missing means preserve the existing default, never borrow another model's override."""
    effort = values.get(model)
    if effort is not None and effort not in efforts:
        raise ValueError("The configured Fusion reasoning level is no longer supported by this model.")
    return effort
