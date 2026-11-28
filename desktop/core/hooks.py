"""Hooks of the application itself: the places where WittyCode stands between the user and each engine's conversation.

Droid, Codex and Devin each have hooks of their own (files in their configuration, run by the engine); these are the
application's, the same for all three, and need nothing written to any engine's configuration. A prompt hook may add
context for the model (``PromptContext``). The managed-test prompt hook directs each session to use its own foreground
runner; only that runner owns its completion and cleanup (``core/test_runs.py``).

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
    internal: bool = False
    """Sent to an internal session (a Fusion expert's delegation), not to one the user is talking with: hooks meant only
    for the user's own messages leave these to their own protocol."""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """What a hook adds: ``text`` for the model (it goes after the typed text), ``notice`` one line for the user (the page
    shows it in the turn), ``ids`` what was given (the conversation remembers it once the turn is done)."""

    text: str = ""
    notice: str = ""
    ids: frozenset[str] = frozenset()


PromptHook = Callable[[PromptEvent], "PromptContext | None"]


def managed_test_prompt(_event: PromptEvent) -> PromptContext:
    from desktop.core.memory.launch import console_python
    from desktop.core import test_runs

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    runner = f"& {quote(console_python())} {quote(str(Path(test_runs.__file__).resolve()))} -- "
    return PromptContext(text=(
        "If you run tests, use the built-in managed test runner from this session's project root "
        f"(PowerShell: {runner}<executable> <args>), or a project test entry point that already manages its own run. "
        "Keep test processes in the foreground. Put disposable files in WITTYCODE_TEST_WORK and only selected evidence "
        "in WITTYCODE_TEST_EVIDENCE. Each session, thread, and delegated agent runs and cleans up only its own tests. "
        "Do not run another session inside this runner, pass it another run's directory, or clean up for a parent, child "
        "or sibling agent. Never scan or delete pre-existing files or session history. Report cleanup failures."
    ))


@dataclass(frozen=True, slots=True)
class BuiltinPromptHook:
    """A client hook that is both runnable and visible in the settings catalog."""

    id: str
    name: str
    description: str
    handler: PromptHook

    def __call__(self, event: PromptEvent) -> PromptContext | None:
        return self.handler(event)


MANAGED_TEST_HOOK = BuiltinPromptHook(
    id="wittycode.test-cleanup",
    name="测试收尾",
    description="要求每个会话使用独立的测试入口，只清理本次测试的临时文件，保留证据和结论。不会代其他会话或子代理清理。",
    handler=managed_test_prompt,
)


class Hooks:
    def __init__(self) -> None:
        self._prompt: list[PromptHook] = []

    def on_prompt(self, hook: PromptHook) -> None:
        self._prompt.append(hook)

    def builtin_prompt_hooks(self) -> list[dict[str, str]]:
        return [{"id": hook.id, "name": hook.name, "description": hook.description}
                for hook in self._prompt if isinstance(hook, BuiltinPromptHook)]

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
