"""WittyCode's bundled official skills: real ``SKILL.md`` files that ship inside the client (``desktop/assets/skills``).

The first one is ``numbered-reply``: a standing output-format rule for replies the user reads — a conclusion-first
paragraph, then each matter as its own numbered item, questions numbered as well. It is on by default and the user
switches it under 设置 → 插件 → Skills (``AppSettings.numbered_reply``).

The automatic injection reads only the packaged file. A project or personal skill that happens to share the name is
never loaded in its place and keeps its own ``/name`` semantics; a message that explicitly names such a skill is left
to it (the explicit instruction wins). Messages of an internal session — a Fusion expert's delegation,
``PromptEvent.internal`` — get none of it: their protocol is their own.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from desktop.core.hooks import PromptContext, PromptEvent, PromptHook

NAME = "numbered-reply"
DIR = Path(__file__).resolve().parents[2] / "assets" / "skills"
"""The folder of the bundled skills inside the client package (copied whole into the release, ``scripts/build_release``)."""
FILE = DIR / NAME / "SKILL.md"


def body() -> str:
    """The bundled skill's instructions, without its front matter; empty when the file is unreadable."""
    from desktop.core.harness import skills

    text = skills._read(FILE)
    return skills.split_front_matter(text)[1].strip()[: skills.BODY_LIMIT] if text is not None else ""


def context() -> PromptContext | None:
    text = body()
    if not text:
        return None
    return PromptContext(text=f'<skill name="{NAME}" official="wittycode">\n{text}\n</skill>')


def prompt_hook(enabled: Callable[[], bool]) -> PromptHook:
    """The reply-format injection shared by every engine's user-facing message; ``enabled`` is read at each send so the
    settings switch takes hold of the very next message."""

    def hook(event: PromptEvent) -> PromptContext | None:
        if event.internal or not enabled():
            return None
        if "/" + NAME in event.text:
            from desktop.core.harness import skills

            if any(skill.name == NAME for skill in skills.named_in(event.text, skills.discover(event.project))):
                return None  # the user explicitly named their own skill of this name; it speaks, not the bundled one
        return context()

    return hook
