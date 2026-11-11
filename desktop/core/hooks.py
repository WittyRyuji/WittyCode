"""Hooks of the application itself: the places where WittyCode stands between the user and each engine's conversation.

Droid, Codex and Devin each have hooks of their own (files in their configuration, run by the engine); these are the
application's, the same for all three, and need nothing written to any engine's configuration. For now one place has a
hook, the one before a message is sent: a hook may add context for the model (``PromptContext``). The shared memory is the
first to use it (``core/memory/launch.py``): it finds the notes that bear on the message.

A hook never stops a message from going out: one that raises is logged and left out.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PromptEvent:
    """A message about to be sent."""

    engine: str
    """``droid`` | ``codex`` | ``devin``."""
    project: Path | str
    text: str
    """What the user typed."""
    seen: frozenset[str] = frozenset()
    """What hooks already gave this conversation (ids of notes): a hook need not give it again."""
    first: bool = False
    """The first message of a new conversation (it is still the first when an earlier try of it failed). Context meant for
    the start of a conversation is given only then; after that the model asks for what it needs."""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """What a hook adds: ``text`` for the model (it goes after the typed text), ``notice`` one line for the user (the page
    shows it in the turn), ``ids`` what was given (the conversation remembers it once the turn is done)."""

    text: str = ""
    notice: str = ""
    ids: frozenset[str] = frozenset()


PromptHook = Callable[[PromptEvent], "PromptContext | None"]


class Hooks:
    def __init__(self) -> None:
        self._prompt: list[PromptHook] = []

    def on_prompt(self, hook: PromptHook) -> None:
        self._prompt.append(hook)

    def prompt(self, event: PromptEvent) -> PromptContext:
        """What every hook adds to this message, joined."""
        texts: list[str] = []
        notices: list[str] = []
        ids: set[str] = set()
        for hook in self._prompt:
            try:
                result = hook(event)
            except Exception:
                log.exception("prompt hook failed")
                continue
            if result is None or not result.text.strip():
                continue
            texts.append(result.text)
            if result.notice:
                notices.append(result.notice)
            ids |= result.ids
        return PromptContext("\n".join(texts), " · ".join(notices), frozenset(ids))
