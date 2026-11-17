"""The language the models think in.

The user reads the models' thinking in the page (folded under each answer) and writes Chinese; left alone, most models think
in English whatever the conversation's language. A rule asks for Chinese throughout the turn, without an English version
followed by a translation: in the own loop's instructions (``harness/prompt.py``), and, for external engines, after each
message, including steering (the prompt hook, ``core/hooks``; hidden by ``core/context_envelope``). Compliance is up to the model; a
reasoning summary that the service writes itself may stay in English.
"""

from __future__ import annotations

from collections.abc import Callable

from desktop.core.hooks import PromptContext, PromptEvent, PromptHook

THINK_CHINESE = (
    "Throughout every turn, write all user-visible thinking, reasoning summaries and progress updates directly in "
    "Simplified Chinese, including after tool calls and when resuming or continuing work. Apply this to every step, not "
    "just the first message. Express each update only once in Chinese; do not write an English version first or append a "
    "translation. Keep code, commands, paths and identifiers unchanged. Answer in the language the user writes in."
)
OWN_LOOP = "chatgpt"
"""The engine whose instructions are WittyCode's own: the sentence is in them already."""


def rules_for(answer: str = "auto", thinking: str = "", *, legacy_chinese: bool = True) -> str:
    if answer == "auto" and not thinking and not legacy_chinese:
        return "The Chinese thinking-language preference is off. Answer in the user's language."
    thinking = thinking or ("zh-CN" if legacy_chinese else "auto")
    if answer == "auto" and thinking == "zh-CN":
        return THINK_CHINESE
    response = {"zh-CN": "Answer in Simplified Chinese.", "en": "Answer in English."}.get(answer, "Answer in the language the user writes in.")
    thought = (THINK_CHINESE.rsplit("Answer in", 1)[0] if thinking == "zh-CN" else
               "Write all user-visible thinking and progress updates directly in English, including after tools; do not repeat a translation. " if thinking == "en" else
               "The Chinese thinking-language preference is off. Use the user's language for visible thinking and progress. ")
    return "These explicit language preferences override generic language defaults. " + thought + response


def prompt_hook(enabled: Callable[[], bool], answer: Callable[[], str] = lambda: "auto", thinking: Callable[[], str] = lambda: "") -> PromptHook:
    """The hook for the other engines; ``enabled`` is read at every message (the setting may change between two)."""

    def hook(event: PromptEvent) -> PromptContext | None:
        if event.engine == OWN_LOOP or not enabled() and answer() == "auto" and thinking() in ("", "auto"):
            return None
        return PromptContext(text=rules_for(answer(), thinking(), legacy_chinese=enabled()))

    return hook
