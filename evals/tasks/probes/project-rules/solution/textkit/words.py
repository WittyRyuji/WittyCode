"""Words of a text."""

import re

_WORD = re.compile(r"[A-Za-z0-9]+")


def words(text: str) -> list[str]:
    """The words of a text, in order."""
    return _WORD.findall(text)


def slugify(title: str) -> str:
    """The words of a title in lower case, joined by hyphens."""
    return "-".join(word.lower() for word in words(title))
