"""Remove known secret shapes and flag instruction-like data before storage."""
from __future__ import annotations

import re
from dataclasses import dataclass

from desktop.core.memory.safety import _PATTERNS

_ROLE = re.compile(
    r"</?(?:system|user|assistant|developer)>|\[/?(?:system|user|assistant|developer|inst)\]"
    r"|<\|(?:im_start|im_end|start_header_id|end_header_id|system|user|assistant|developer)\|>"
    r"|^[ \t]*(?:#{1,6}[ \t]+)?(?:system|user|assistant|developer)[ \t]*:"
    r"|^[ \t]*#{1,6}[ \t]+(?:system|user|assistant|developer)[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
_OVERRIDE = re.compile(
    r"(?<!do not )(?<!don't )(?<!never )\b(?:ignore|disregard|forget)\s+"
    r"(?:(?:all|any)(?:\s+of)?\s+)?(?:(?:the|your|these|those)\s+)?"
    r"(?:(?:previous|prior|earlier|above|preceding)\s+(?:(?:system|developer|user)\s+)?(?:instructions?|directives?)\b"
    r"|(?:instructions?|directives?)\s+(?:from\s+)?above\b)"
    r"|(?:忽略|无视|忘记|忘掉|不理会)(?:所有|全部)?(?:之前|以前|此前|先前|前面|以上|上述|上文)"
    r"(?:的)?(?:所有|全部)?(?:的)?(?:指令|指示|命令)",
    re.IGNORECASE,
)
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
_SUSPECT = (
    ("turn marker", _ROLE),
    ("instruction override", _OVERRIDE),
    ("invisible or direction-control character", _INVISIBLE),
)


@dataclass(frozen=True, slots=True)
class Scrubbed:
    text: str
    secrets: tuple[str, ...]
    suspect: tuple[str, ...]


def scrub(text: str) -> Scrubbed:
    """Mask all matches and flag suspect patterns without rewriting instructions."""
    spans: list[tuple[int, int]] = []
    kinds = []
    for kind, pattern in _PATTERNS:
        matches = [match.span() for match in pattern.finditer(text)]
        if matches:
            kinds.append(kind)
            spans.extend(matches)
    suspect = tuple(reason for reason, pattern in _SUSPECT if pattern.search(text))
    if not spans:
        return Scrubbed(text, (), suspect)

    # Collect matches on the original so one pattern cannot hide an overlapping secret.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, pos = [], 0
    for start, end in merged:
        parts.extend((text[pos:start], "[secret removed]"))
        pos = end
    return Scrubbed("".join(parts) + text[pos:], tuple(kinds), suspect)
