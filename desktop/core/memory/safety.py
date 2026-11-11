"""What must never be written into the shared memory: secrets.

The memory is read back into the context of other conversations (other engines, other projects for the global part), so
a key that got into it once would be handed to every model afterwards. The check is a net with wide meshes: it looks
for the shapes keys, tokens and passwords usually have. A hit is refused with a sentence the model can pass on.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an API key (sk-…)", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("a key from a token plan (tp-… / ttp-…)", re.compile(r"\bt{1,2}p-[A-Za-z0-9]{20,}")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("a bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/\-]{20,}=*", re.IGNORECASE)),
    (
        "a password or secret",
        re.compile(r"\b(?:pass(?:word|wd)?|secret|token|api[_\- ]?key|access[_\- ]?key|credential)s?\b\s*[:=]\s*[\"']?[^\s\"']{8,}", re.IGNORECASE),
    ),
    # (a long run of letters and digits with both cases and a digit in it: random, not a word or a commit hash, which
    # a note may well quote; a plain hex string is not matched for the same reason)
    (
        "a long random-looking string",
        re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"),
    ),
)


def find_secret(*texts: str) -> str | None:
    """What kind of secret the text seems to hold (``None``: nothing found)."""
    for text in texts:
        for kind, pattern in _PATTERNS:
            if pattern.search(text):
                return kind
    return None
