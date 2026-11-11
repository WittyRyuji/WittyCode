"""The context WittyCode adds to a message for the model, and how it is told from what the user typed.

It goes **after** the typed text, in one tagged block: an engine names a conversation after its first message (Codex and
Devin from the text, Droid from the first exchange), and a note about the shared memory in front of it made every new
conversation start with "[Shared memory of the user's coding agents ..." in the sidebar. The saved conversation holds the
block too, so the readers of saved history (``strip``) take it out again; the page only ever shows what was typed.
"""

from __future__ import annotations

import re

OPEN = "<wittycode-context>"
CLOSE = "</wittycode-context>"

# (an unclosed block runs to the end: a title or preview may have been cut in the middle of it)
_BLOCK = re.compile(r"\n*" + re.escape(OPEN) + r".*?(?:" + re.escape(CLOSE) + r"|\Z)", re.DOTALL)
# The first version put the note in front of the message, with no tags; conversations made then are read the same way.
_LEGACY = re.compile(r"\A\[Shared memory of the user's coding agents is available:.*?\n\n", re.DOTALL)


def wrap(*parts: str) -> str:
    """What follows the typed text: the parts (the empty ones left out) in one block; nothing when there is nothing."""
    body = "\n".join(part.strip() for part in parts if part and part.strip())
    return f"\n\n{OPEN}\n{body}\n{CLOSE}" if body else ""


def strip(text: str) -> str:
    """``text`` as the user typed it: the block WittyCode added taken out."""
    if OPEN not in text and not text.startswith("[Shared memory"):
        return text
    return _LEGACY.sub("", _BLOCK.sub("", text), count=1)
