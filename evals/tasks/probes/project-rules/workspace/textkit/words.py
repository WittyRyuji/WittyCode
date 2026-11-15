"""Words of a text."""

import re

_WORD = re.compile(r"[A-Za-z0-9]+")


def words(text: str) -> list[str]:
    """The words of a text, in order."""
    return _WORD.findall(text)
