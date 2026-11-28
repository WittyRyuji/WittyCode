"""The application's own cache: what was last known, so the window shows it before anything is connected.

Lives in ``<settings folder>\\cache\\`` (``%APPDATA%\\WittyCode\\cache``):

* ``index.json``: the saved-session rows of every project, the model list, the Codex account (plan, limits) and the
  Droid version, as they were last seen;
* ``sessions\\<id>.json``: one session's conversation as the page shows it (the view model's turns), plus metadata.
* ``session-pages\\<id>.json``: a small preview and byte offsets into the matching session JSON; older pages are read on demand.

Nothing here is the truth: the engines are. The page is shown the cached state first, and the real state replaces it
as soon as it arrives (a moment later, in the background). A file that is missing, unreadable, from another version or
too large is simply not used; writing is atomic (a temporary file, then a rename) so a crash cannot leave half a file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from desktop.core.turn_window import PAGE_SIZE

log = logging.getLogger("wittycode.cache")

CACHE_VERSION = 1
MAX_SESSION_BYTES = 12 * 1024 * 1024
"""A conversation bigger than this is not cached (it would take longer to read than to load the real history)."""
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _write_atomic(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8") if isinstance(text, str) else text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class Cache:
    """``Cache(None)`` caches nothing (tests, the offline replay)."""

    def __init__(self, folder: Path | None) -> None:
        self.folder = folder

    @property
    def enabled(self) -> bool:
        return self.folder is not None

    # ------------------------------------------------------------------ the index

    @property
    def index_path(self) -> Path | None:
        return None if self.folder is None else self.folder / "index.json"

    def load_index(self) -> dict[str, Any]:
        return self._read(self.index_path) or {}

    def save_index(self, index: dict[str, Any]) -> None:
        path = self.index_path
        if path is None:
            return
        self._write(path, {"v": CACHE_VERSION, **index})

    # ------------------------------------------------------------------ one session

    def session_path(self, key: str) -> Path | None:
        return None if self.folder is None else self.folder / "sessions" / f"{_UNSAFE.sub('_', key)}.json"

    def load_session(self, key: str) -> dict[str, Any] | None:
        return self._read(self.session_path(key))

    @staticmethod
    def _page_index(path: Path) -> Path:
        return path.parent.parent / "session-pages" / path.name

    @staticmethod
    def _same_snapshot(stream: BinaryIO, index: dict[str, Any]) -> bool:
        stamp = index.get("stamp")
        if not isinstance(stamp, str) or re.fullmatch(r"[a-f0-9]{32}", stamp) is None:
            return False
        prefix = f'{{"stamp":"{stamp}",'.encode("ascii")
        return stream.read(len(prefix)) == prefix and stream.seek(0, os.SEEK_END) == index.get("size")

    def load_session_preview(self, key: str) -> dict[str, Any] | None:
        path = self.session_path(key)
        if path is None:
            return None
        index = self._read(self._page_index(path))
        if index is not None and index.get("key") == key:
            try:
                with path.open("rb") as stream:
                    if self._same_snapshot(stream, index):
                        return index
            except OSError:
                pass
        # Older versions and an interrupted two-file update still have the original valid JSON snapshot.
        return self.load_session(key)

    def load_session_page(self, key: str, stamp: str, end: int) -> list[dict[str, Any]] | None:
        path = self.session_path(key)
        index = self._read(self._page_index(path)) if path is not None else None
        if path is None or index is None or index.get("key") != key or index.get("stamp") != stamp:
            return None
        offsets = index.get("offsets")
        if not isinstance(offsets, list) or not 0 < end < len(offsets):
            return None
        lo, hi = offsets[max(0, end - PAGE_SIZE)], offsets[end]
        if not (type(lo) is type(hi) is int and 0 < lo < hi <= MAX_SESSION_BYTES):
            return None
        try:
            with path.open("rb") as stream:
                if not self._same_snapshot(stream, index):
                    return None
                stream.seek(lo)
                page = json.loads(b"[" + stream.read(hi - lo).rstrip(b",") + b"]")
                return page if all(isinstance(turn, dict) and isinstance(turn.get("id"), str) for turn in page) else None
        except (OSError, ValueError):
            return None

    def save_session(self, key: str, data: dict[str, Any]) -> bool:
        """Normal JSON for compatibility, with a separate index that can read just the requested UTF-8 range."""
        path = self.session_path(key)
        if path is None:
            return False
        turns = data.get("turns")
        if not isinstance(turns, list):
            return self._write(path, {"v": CACHE_VERSION, "key": key, **data}, limit=MAX_SESSION_BYTES)
        index_path = self._page_index(path)
        stamp = uuid.uuid4().hex
        meta = {"v": CACHE_VERSION, "key": key, **{k: v for k, v in data.items() if k != "turns"}}
        try:
            encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            blob = bytearray(encode({"stamp": stamp, **meta})[:-1] + b',"turns":[')
            offsets = []
            for i, turn in enumerate(turns):
                if i:
                    blob.extend(b",")
                offsets.append(len(blob))
                blob.extend(encode(turn))
                if len(blob) + 2 > MAX_SESSION_BYTES:
                    self.forget_session(key)
                    return False
            offsets.append(len(blob))
            blob.extend(b"]}")
            if len(blob) > MAX_SESSION_BYTES:
                self.forget_session(key)
                return False
            _write_atomic(path, bytes(blob))
            start = max(0, len(turns) - PAGE_SIZE)
            plans = (turn.get("plan") for turn in reversed(turns[:start]) if isinstance(turn, dict))
            plan = next((value for value in plans if value is not None), None)
            self._write(index_path, {**meta, "stamp": stamp, "size": len(blob), "offsets": offsets,
                                     "pageStart": start, "turns": turns[start:], "olderPlan": plan})
            return True
        except (OSError, TypeError, ValueError) as error:
            log.warning("could not write cache file %s: %s", path.name, error)
            return False

    def forget_session(self, key: str) -> None:
        path = self.session_path(key)
        if path is not None:
            self.forget_stale(path)
            self.forget_stale(self._page_index(path))

    # ------------------------------------------------------------------ files

    def _read(self, path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            log.warning("cache file %s is unreadable (%s): not used", path.name, error)
            return None
        if not isinstance(data, dict) or data.get("v") != CACHE_VERSION:
            return None
        return data

    def _write(self, path: Path, data: dict[str, Any], *, limit: int | None = None) -> bool:
        try:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if limit is not None and len(text) > limit:
                log.info("cache file %s would be %d bytes: not written", path.name, len(text))
                self.forget_stale(path)
                return False
            _write_atomic(path, text)
            return True
        except (OSError, TypeError, ValueError) as error:
            log.warning("could not write cache file %s: %s", path.name, error)
            return False

    @staticmethod
    def forget_stale(path: Path) -> None:
        """An older, smaller copy of a file that can no longer be written is worse than none: it shows old content."""
        try:
            path.unlink()
        except OSError:
            pass
