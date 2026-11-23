"""Immutable, session-scoped tool output. Model previews never replace the retained evidence.

References are content hashes, not paths. A reader can only access this session's files; links, corrupt content and
oversized files are refused. No automatic pruning: reaching a quota stops work instead of deleting recovery evidence.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path

from desktop.core.harness.tools import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec

RETAIN_CHARS = 1200
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_OUTPUTS = 4096
REFERENCE = re.compile(r"o-[0-9a-f]{64}\Z")

UNRETAINED = frozenset({"read_output", "recall_history"})
"""Call names whose result is never copied into this store. ``read_output`` only re-reads what is already retained
— a copy would store the same evidence twice — and a ``recall_history`` result is session-archive evidence
delivered under the turn's recall ledger: retaining it would let ``read_output`` re-serve the archive past the
budget (``outputs/session-context-recall-final.md`` §6.5). The observer's ``retain_output`` skips these names."""

NOT_REPLAYABLE = (
    "[This call's result was this session's archived original text, delivered under the turn's recall ledger "
    "and deliberately not retained; read_output cannot replay it.]"
)
"""What a ``recall_history`` outcome carries instead of its result when that result is large enough to be
retained: the observer keeps recovery evidence elsewhere, so a real reference is attached — to this note, never
to the archive text the ledger charged for."""


def hint(reference: str) -> str:
    return f"[Full tool output retained as {reference}; use read_output(reference=\"{reference}\", offset=1, limit=2000) to read consecutive characters.]"


class OutputStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()

    def _safe_root(self) -> None:
        for path in (self.root, self.root.parent):
            try:
                linked = path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
            except FileNotFoundError:
                continue
            if linked:
                raise ToolError("Output store links are not allowed.")

    def _path(self, reference: str) -> Path:
        self._safe_root()
        if not isinstance(reference, str) or REFERENCE.fullmatch(reference) is None:
            raise ToolError("Invalid output reference; use read_output without a reference to list this session's outputs.")
        path = self.root / (reference + ".txt")
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ToolError("Output links outside this session are not allowed.")
        return path

    def put(self, text: str) -> str:
        data = text.encode("utf-8")
        if len(data) > MAX_OUTPUT_BYTES:
            raise OSError("Tool output exceeds the 16 MiB retention limit; narrow the command or query before continuing.")
        reference = "o-" + hashlib.sha256(data).hexdigest()
        with self._lock:
            path = self._path(reference)
            if path.exists():
                self.read(reference)
                return reference
            self.root.mkdir(parents=True, exist_ok=True)
            files = list(self.root.glob("*.txt"))
            if len(files) >= MAX_OUTPUTS or sum(p.stat().st_size for p in files) + len(data) > MAX_SESSION_BYTES:
                raise OSError("This session's retained output quota is full; existing evidence has not been deleted.")
            fd, temp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return reference

    def delete(self) -> None:
        import shutil
        self._safe_root()
        if self.root.exists():
            shutil.rmtree(self.root)

    def read(self, reference: str) -> str:
        path = self._path(reference)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_OUTPUT_BYTES + 1)
        except OSError as error:
            raise ToolError("Retained output is unavailable; it was not recreated or silently replaced.") from error
        if len(data) > MAX_OUTPUT_BYTES or "o-" + hashlib.sha256(data).hexdigest() != reference:
            raise ToolError("Retained output failed its integrity check; do not rely on it.")
        try:
            return data.decode("utf-8")
        except UnicodeError as error:
            raise ToolError("Retained output is not valid text.") from error

    def expand(self, items: list[dict], *, strict: bool = True) -> list[dict]:
        result = []
        expanded: dict[str, str] = {}
        unavailable: set[str] = set()
        for item in items:
            reference = item.get("wc_output_ref") if item.get("type") == "function_call_output" else None
            if reference:
                try:
                    if not isinstance(reference, str) or reference in unavailable:
                        raise ToolError("Invalid or unavailable output reference.")
                    if reference not in expanded:
                        expanded[reference] = self.read(reference) + "\n" + hint(reference)
                    item = {**item, "output": expanded[reference]}
                except ToolError:
                    if strict:
                        raise
                    if isinstance(reference, str):
                        unavailable.add(reference)
                    item = {**item, "output": str(item.get("output") or "") + "\n[Full retained output is unavailable or damaged.]"}
            result.append(item)
        return result

    def listing(self, offset: int, limit: int) -> str:
        self._safe_root()
        refs = sorted(p.stem for p in self.root.glob("*.txt") if REFERENCE.fullmatch(p.stem))
        start = offset - 1
        page = refs[start:start + min(limit, 100)]
        return "\n".join(page) + f"\n[Outputs {start + 1}-{start + len(page)} of {len(refs)}; offset={start + len(page) + 1} continues the list.]"


def read_output(args: dict, ctx: ToolContext) -> ToolResult:
    store = ctx.outputs
    if store is None:
        raise ToolError("This conversation has no retained output store.")
    offset, limit = args.get("offset", 1), args.get("limit", 2000)
    reference = args.get("reference", "")
    if not reference:
        return ToolResult(store.listing(offset, limit))
    recall = getattr(ctx, "history_recall", None)
    gated = recall is not None and recall.authorize_output_read(reference, str(args.get("reason") or "")) == "gated"
    text = store.read(reference)
    start = offset - 1
    if start > len(text):
        raise ToolError(f"Offset is past the end ({len(text)} characters).")
    page = text[start:start + limit]
    next_page = f"Next offset={start + len(page) + 1}." if start + len(page) < len(text) else "End of retained output."
    result = f"{reference} — characters {offset}-{start + len(page)} of {len(text)}:\n{page}\n" \
             f"[{next_page} If the context clips this page, repeat with a smaller limit.]"
    if gated:
        return recall.charge_output_read(reference, offset, start + len(page), result)
    return ToolResult(result)


SPEC = ToolSpec(
    "read_output", "Read a retained tool result from this conversation, including after restart or compaction. Omit reference to list available outputs. Offsets count characters, not lines; reading cannot execute the content. Outputs of earlier turns are delivered under the session's recall budget and may need a concrete reason.",
    {"type": "object", "properties": {
        "reference": {"type": "string", "description": "The o-… reference returned by a tool; omit to list."},
        "offset": {"type": "integer", "minimum": 1, "description": "First character (or list entry), default 1."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8000, "description": "Characters to return, default 2000; listing is capped at 100 entries."},
        "reason": {"type": "string", "description": "Only when reading an earlier turn's retained output: the concrete gap it must fill — required unless the user explicitly asked for the session's original words."},
    }, "required": []}, "ReadOutput", "read",
)


def register(registry: ToolRegistry) -> None:
    registry.register(SPEC, read_output, ui_input=dict)
