"""The language the models think in.

The user reads the models' thinking in the page (folded under each answer) and writes Chinese; left alone, most models think
in English whatever the conversation's language. One sentence asks for Chinese: in the own loop's instructions
(``harness/prompt.py``), and, for the engines whose instructions are their own, after each message (the application's prompt
hook, ``core/hooks``; the page never shows it, ``core/context_envelope``). Whether a model complies is up to the model; a
reasoning summary that the service writes itself may stay in English.
"""

from __future__ import annotations

from collections.abc import Callable

from desktop.core.hooks import PromptContext, PromptEvent, PromptHook

THINK_CHINESE = (
    "Write your thinking (the reasoning before and between your steps) in Simplified Chinese, and answer in the language the "
    "user writes in."
)
OWN_LOOP = "chatgpt"
"""The engine whose instructions are WittyCode's own: the sentence is in them already."""


def prompt_hook(enabled: Callable[[], bool]) -> PromptHook:
    """The hook for the other engines; ``enabled`` is read at every message (the setting may change between two)."""

    def hook(event: PromptEvent) -> PromptContext | None:
        if event.engine == OWN_LOOP or not enabled():
            return None
        return PromptContext(text=THINK_CHINESE)

    return hook
