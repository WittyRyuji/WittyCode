"""What Codex has written into a thread's rollout file: the token figures and the settings of its last turn.

Codex does not report token use when a thread is resumed (``thread/tokenUsage/updated`` comes with the first turn that
runs), so a thread nobody has talked to since the app started would show no token total and no context size. The
rollout file has them: its ``token_count`` events carry the same ``total`` / ``last`` / ``modelContextWindow`` as that
notification, and each ``turn_context`` line says what the turn ran with. Both are read from the END of the file (the
newest are last, and a long thread's file is many megabytes).

The file's format is Codex's own and not documented; every surprise (a missing file, a line that is not JSON, a field
that is not there) just means "not known", and the caller does without.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.codex.policy import normalize_level

log = logging.getLogger("desktop.codex.rollout")

CHUNK_BYTES = 256 * 1024
SCAN_LIMIT_BYTES = 48 * 1024 * 1024
"""How much of the end of the file is looked through (a token_count is written after every model call)."""
_TOKEN_COUNT = b'"type":"token_count"'
_TURN_CONTEXT = b'"type":"turn_context"'


@dataclass(frozen=True, slots=True)
class RolloutFacts:
    model: str = ""
    effort: str = ""
    level: str | None = None
    """The page's permission level the last turn ran with (``readonly`` / ``workspace`` / ``full``)."""
    token_usage: dict[str, Any] | None = None
    """The newest ``tokenUsage`` in the form of ``thread/tokenUsage/updated``: ``{total, last, modelContextWindow}``."""


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _breakdown(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {_camel(str(key)): value for key, value in raw.items()}


def _lines_from_end(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        carry = b""
        scanned = 0
        while position > 0 and scanned < SCAN_LIMIT_BYTES:
            step = min(CHUNK_BYTES, position)
            position -= step
            stream.seek(position)
            block = stream.read(step) + carry
            scanned += step
            first, *lines = block.split(b"\n")
            carry = first  # (its start is in the block before this one)
            for line in reversed(lines):
                if line.strip():
                    yield line
        if position == 0 and carry.strip():
            yield carry


def _token_usage(line: bytes) -> dict[str, Any] | None:
    payload = _payload(line)
    info = payload.get("info") if payload.get("type") == "token_count" else None
    if not isinstance(info, Mapping):
        return None
    total, last = _breakdown(info.get("total_token_usage")), _breakdown(info.get("last_token_usage"))
    if total is None:
        return None
    usage: dict[str, Any] = {"total": total}
    if last is not None:
        usage["last"] = last
    if isinstance(info.get("model_context_window"), (int, float)):
        usage["modelContextWindow"] = info["model_context_window"]
    return usage


def _payload(line: bytes) -> Mapping[str, Any]:
    try:
        record = json.loads(line)
    except ValueError:
        return {}
    payload = record.get("payload") if isinstance(record, Mapping) else None
    return payload if isinstance(payload, Mapping) else {}


def _turn_settings(line: bytes) -> tuple[str, str, str | None]:
    payload = _payload(line)
    model = payload.get("model")
    effort = payload.get("effort")
    if not isinstance(effort, str):
        mode = payload.get("collaboration_mode")
        settings = mode.get("settings") if isinstance(mode, Mapping) else None
        effort = settings.get("reasoning_effort") if isinstance(settings, Mapping) else ""
    policy = payload.get("sandbox_policy")
    kind = policy.get("type") if isinstance(policy, Mapping) else None
    return (model if isinstance(model, str) else "", effort if isinstance(effort, str) else "", normalize_level(kind))


def read_rollout(path: str | os.PathLike[str] | None) -> RolloutFacts | None:
    """The newest token figures and turn settings in a rollout file; ``None`` when the file says neither."""
    if not path:
        return None
    usage: dict[str, Any] | None = None
    settings: tuple[str, str, str | None] | None = None
    try:
        for line in _lines_from_end(Path(path)):
            if usage is None and _TOKEN_COUNT in line:
                usage = _token_usage(line)
            elif settings is None and _TURN_CONTEXT in line:
                settings = _turn_settings(line)
            if usage is not None and settings is not None:
                break
    except OSError as error:
        log.info("could not read the rollout %s: %s", path, error)
        return None
    if usage is None and settings is None:
        return None
    model, effort, level = settings or ("", "", None)
    return RolloutFacts(model=model, effort=effort, level=level, token_usage=usage)
