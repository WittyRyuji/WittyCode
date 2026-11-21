"""The sidebar's ordering helpers: one pinned zone ahead of the projects.

``AppSettings.pinned_sessions`` holds the pinned rows' persistent keys in display order across
all projects — it never follows a project's own reordering, so folding, moving or removing a
project cannot shuffle the pins. ``AppSettings.session_pins`` keeps the same pins grouped per
project: older builds read only that, and files written before the global list existed migrate
through ``merge_pins``. ``move_relative`` is the drag both session and project reordering share.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


def move_relative(items: list[str], source: str, target: str, after: bool) -> None:
    """Move ``source`` next to ``target`` — before it, or after when ``after``. Both are in ``items``."""
    items.remove(source)
    items.insert(items.index(target) + int(after), source)


def pin_mirror(pinned: Iterable[str], known: Iterable[str]) -> list[str]:
    """One project's share of the global pin list, in the global order."""
    keep = set(known)
    return [item for item in pinned if item in keep]


def merge_pins(
    pinned: Iterable[str],
    session_pins: Mapping[str, Iterable[str]],
    projects: Iterable[str],
    key_of: Callable[[str], str],
) -> list[str]:
    """The global pin list of a loaded file: ``pinned_sessions`` first, then legacy per-project pins.

    Legacy pins of projects still on the list come first, in the project's own order; pins of
    folders no longer listed follow, so a removed project never lost its pins. Nothing is taken
    twice and empties fall away.
    """
    merged = list(dict.fromkeys(item for item in pinned if item))
    seen = set(merged)
    handled: set[str] = set()

    def take(keys: Iterable[str]) -> None:
        for item in keys:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)

    for folder in projects:
        key = key_of(folder)
        handled.add(key)
        take(session_pins.get(key, ()))
    for key, keys in session_pins.items():
        if key not in handled:
            take(keys)
    return merged
