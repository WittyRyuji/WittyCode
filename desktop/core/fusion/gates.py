"""Independently reversible Fusion stages."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from desktop.core.settings import AppSettings

GATES = ("drive", "brief", "report", "route", "acceptance", "inject", "map", "observer", "v3", "unit_plan")
DEFAULT: bool | frozenset[str] = True
"""What a gate is when the settings do not name it. Tests narrow this to the gates they exercise."""


def enabled(settings: AppSettings, name: str) -> bool:
    if name not in GATES:
        raise ValueError(f"Unknown Fusion gate: {name}")
    return settings.fusion_gates.get(name, DEFAULT if isinstance(DEFAULT, bool) else name in DEFAULT)
