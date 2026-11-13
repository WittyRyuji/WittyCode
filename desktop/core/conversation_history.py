"""Durable, engine-neutral history preceding a native session; never pickle or executable object loading.

Only successful engine changes write here. The current segment remains owned by its engine; these closed turns and
its starting ledger cannot be discarded like the UI cache. Native aliases live in AppSettings.session_links.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from desktop.core import transcript as tr
from desktop.core.attachments import Attachment
from desktop.core.cache import _write_atomic
from desktop.core.diffs import DiffLine, FileDiff
from desktop.core.handoff import Handoff
from desktop.core.prompts import ActionView, OptionView, QuestionView
from desktop.core.toolinfo import Category, TodoEntry

_TYPES = {cls.__name__: cls for cls in (
    tr.Turn, tr.UserBlock, tr.TextBlockView, tr.ActivityBlock, tr.ToolItem, tr.ThinkingEntry, tr.QABlock,
    tr.ErrorBlock, tr.NoticeBlock, tr.SwitchBlock, tr.PermissionPrompt, tr.QuestionPrompt, tr.PlanSnapshot,
    Attachment, FileDiff, DiffLine, ActionView, OptionView, QuestionView, TodoEntry, Handoff,
)}
_ENUMS = {cls.__name__: cls for cls in (tr.TurnStatus, tr.ToolStatus, tr.PromptStatus, Category)}
_TURN_TRANSIENT = {"usage", "tools", "texts", "thinking", "group_of"}


def _pack(value: Any) -> Any:
    if isinstance(value, Enum):
        return {"enum": type(value).__name__, "value": value.value}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if dataclasses.is_dataclass(value) and type(value).__name__ in _TYPES:
        return {"node": type(value).__name__, "fields": {
            f.name: _pack(getattr(value, f.name)) for f in dataclasses.fields(value)
            if not (isinstance(value, tr.Turn) and f.name in _TURN_TRANSIENT)
        }}
    if isinstance(value, dict):
        return {"map": [[_pack(k), _pack(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return {"tuple" if isinstance(value, tuple) else "list": [_pack(v) for v in value]}
    raise ValueError("unsupported history value")


def _unpack(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "enum" in value:
        return _ENUMS[value["enum"]](value["value"])
    if "bytes" in value:
        return base64.b64decode(value["bytes"], validate=True)
    if "node" in value:
        cls = _TYPES[value["node"]]
        fields = {f.name for f in dataclasses.fields(cls)}
        raw = value["fields"]
        if not isinstance(raw, dict) or not raw.keys() <= fields:
            raise ValueError("invalid history fields")
        node = cls(**{k: _unpack(v) for k, v in raw.items()})
        if isinstance(node, tr.Turn):
            node.tools = {e.tool_use_id: e for b in node.blocks if isinstance(b, tr.ActivityBlock) for e in b.entries if isinstance(e, tr.ToolItem)}
        return node
    if "map" in value:
        return {_unpack(k): _unpack(v) for k, v in value["map"]}
    if "tuple" in value:
        return tuple(_unpack(v) for v in value["tuple"])
    if "list" in value:
        return [_unpack(v) for v in value["list"]]
    raise ValueError("invalid history node")


class ConversationHistory:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _path(self, row: str) -> Path:
        return self.folder / (hashlib.sha256(row.encode("utf-8")).hexdigest() + ".json")

    def save(self, row: str, data: dict[str, Any]) -> None:
        _write_atomic(self._path(row), json.dumps({"version": 1, "row": row, "data": _pack(data)}, ensure_ascii=False))

    def load(self, row: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._path(row).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("row") != row:
            raise ValueError("invalid conversation history")
        data = _unpack(raw["data"])
        if not isinstance(data, dict) or not isinstance(data.get("turns"), list) or not all(isinstance(t, tr.Turn) for t in data["turns"]):
            raise ValueError("invalid conversation turns")
        return data


def prepend(transcript: tr.Transcript, turns: list[tr.Turn], namespace: str) -> None:
    """Install closed turns once, with ids distinct from the new engine's counters and tool ids."""
    if not turns or turns[0] in transcript.turns:
        return
    prefix = "past-" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12] + "-"
    for turn in turns:
        if not turn.id.startswith(prefix):
            turn.id = prefix + turn.id
        turn.from_history = True
        for block in turn.blocks:
            if isinstance(block, tr.UserBlock):
                block.attachments = tuple(dataclasses.replace(a, id=prefix + a.id) if not a.id.startswith(prefix) else a for a in block.attachments)
                for attachment in block.attachments:
                    transcript._attachments[attachment.id] = attachment
    transcript.turns[:0] = turns
    transcript.version += 1
