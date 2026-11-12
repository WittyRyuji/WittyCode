"""The application's own cache: what was last known, so the window shows it before anything is connected.

Lives in ``<settings folder>\\cache\\`` (``%APPDATA%\\WittyCode\\cache``):

* ``index.json``: the saved-session rows of every project, the model list, the Codex account (plan, limits) and the
  Droid version, as they were last seen;
* ``sessions\\<id>.json``: one session's conversation as the page shows it (the view model's turns) with its title,
  settings, token use and context size, written periodically while it runs as well as when it is idle or closes.

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
from pathlib import Path
from typing import Any

log = logging.getLogger("wittycode.cache")

CACHE_VERSION = 1
MAX_SESSION_BYTES = 12 * 1024 * 1024
"""A conversation bigger than this is not cached (it would take longer to read than to load the real history)."""
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
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

    def save_session(self, key: str, data: dict[str, Any]) -> bool:
        """``False`` when it was not written (no cache, too big, or the disk said no)."""
        path = self.session_path(key)
        if path is None:
            return False
        return self._write(path, {"v": CACHE_VERSION, "key": key, **data}, limit=MAX_SESSION_BYTES)

    def forget_session(self, key: str) -> None:
        path = self.session_path(key)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

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
