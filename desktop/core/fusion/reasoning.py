"""Bounded role/model Fusion effort overrides, with legacy per-model fallback."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from desktop.core.fusion.backends import backend_for


ROLES = ("main", "worker", "light")


def reasoning_model(key: str) -> str:
    role, _, model = key.partition(":")
    return model if role in ROLES else key


def reasoning_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 256 or any(
        not isinstance(key, str) or backend_for(reasoning_model(key)) is None
        or len(reasoning_model(key)) > 512
        or not isinstance(effort, str) or not effort.strip() or len(effort) > 64
        for key, effort in value.items()
    ):
        raise ValueError("The Fusion reasoning settings are invalid.")
    return dict(value)


def configured_effort(values: Mapping[str, str], model: str, efforts: Iterable[str], *, role: str = "main") -> str | None:
    """Role-specific values win; old per-model snapshots keep their original behaviour."""
    effort = values.get(f"{role}:{model}", values.get(model))
    if effort is not None and effort not in efforts:
        raise ValueError("The configured Fusion reasoning level is no longer supported by this model.")
    return effort
