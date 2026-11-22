"""The shared per-turn ledger of history recall (final design §6.5).

8,192 tokens is the cumulative delivery ceiling of one real user turn, not of one call: expansion, pagination,
retries and parallel recalls all draw on the same ledger, which is why it survives a backend switch, a retry
and a restart (``<sid>\\turn-budget.json``). Charging is check-and-add under a lock, so two parallel recalls
cannot both fit the same headroom; a record already delivered in the turn costs nothing again, which is also
what keeps a retry or a second caller from returning the same excerpt twice.

The file can be shared across processes, so the whole read-check-write runs inside a byte lock on a sibling
``.lock`` file — never on the ledger itself, which is swapped atomically, and the lock file carries no
payload. The file also names the turn whose book it holds: a ledger object that outlives its turn must not
overwrite a newer turn's page, so a charge is refused once the file's ``turn`` differs both from this
ledger's turn and from the turn it read when it was constructed.

Counting never guesses low: without a trusted tokenizer the ledger charges ``utf8_bound`` — every token of a
byte-level tokenizer encodes at least one byte, so the UTF-8 length is a provable upper bound on the real
count. When the real count cannot be known, a loose estimate is not allowed to let a delivery through.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path

LIMIT = 8192
FILENAME = "turn-budget.json"

_LOCK_WAIT = 10.0     # seconds a charge may queue on the cross-process lock before the book closes
_LOCK_POLL = 0.01

_ABSENT = object()    # the file was read at construction and found missing

_path_locks: dict[Path, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _file_lock(path: Path) -> threading.Lock:
    with _path_locks_guard:
        return _path_locks.setdefault(path, threading.Lock())


@contextmanager
def _ledger_lock(path: Path):
    """Serialize one read-check-write across processes: a byte locked on a sibling ``.lock`` file that is
    never replaced — the same scheme ``plugin_lock`` uses for plugin configuration, carrying no payload."""
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as stream:
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _book(data: object) -> dict | None:
    """The ``{turn, spent}`` page of one file, or ``None`` when anything about it is unproven: wrong shape,
    wrong types, a negative cost, or a ``total`` that does not match what ``spent`` adds up to."""
    if not isinstance(data, Mapping):
        return None
    turn, spent, total = data.get("turn"), data.get("spent"), data.get("total")
    if not isinstance(turn, str) or not isinstance(spent, Mapping) \
            or isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    clean: dict[str, int] = {}
    for key, cost in spent.items():
        if not isinstance(key, str) or isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            return None
        clean[key] = cost
    if sum(clean.values()) != total:
        return None
    return {"turn": turn, "spent": clean}


def utf8_bound(text: str) -> int:
    """A provably conservative token count: no tokenizer emits a token that encodes zero bytes."""
    return len(text.encode("utf-8"))


class RecallLedger:
    """What one turn has already delivered. Pass the same ``path`` and ``turn_id`` after a restart to go on
    where the previous process stopped; a different ``turn_id`` starts a clean book (the turn, not the file,
    is the boundary). ``path=None`` keeps the ledger in memory only.

    ``tokenizer`` may be a callable ``str -> int`` when the engine has a count it trusts; the UTF-8 bound is
    the default and needs no model files.
    """

    def __init__(self, path: Path | str | None = None, turn_id: str = "", *, limit: int = LIMIT,
                 tokenizer: Callable[[str], int] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.turn_id = turn_id
        self.limit = limit
        self._count = tokenizer or utf8_bound
        self._lock = threading.Lock()
        self._spent: dict[str, int] = {}
        self._corrupt = False
        self._stale = False
        self._seen: str | object | None = None   # the turn the file showed at construction
        if self.path is not None:
            try:
                with _file_lock(self.path.resolve()), _ledger_lock(self.path):
                    self._load()
            except OSError:
                self._corrupt = True

    @property
    def total(self) -> int:
        return sum(self._spent.values())

    @property
    def delivered(self) -> frozenset[str]:
        return frozenset(self._spent)

    @property
    def corrupt(self) -> bool:
        """The ledger file exists but cannot be trusted. Fail closed: nothing new is delivered, the file is
        left untouched (it is evidence), and a fresh write never silently resets what was already spent."""
        return self._corrupt

    def _cap(self, room: int | None) -> int:
        """The smaller of the turn ceiling and the context margin the caller still has (design §6.5)."""
        return self.limit if room is None else min(self.limit, max(0, int(room)))

    def remaining(self, room: int | None = None) -> int:
        with self._lock:
            return 0 if self._corrupt or self._stale else max(0, self._cap(room) - self.total)

    def spend(self, key: str, text: str, *, room: int | None = None) -> int | None:
        """Charge ``text`` as ``key`` before it is delivered. Returns the charge — ``0`` when ``key`` was
        already delivered this turn (dedupe, not a second copy) — or ``None`` when it does not fit."""
        cost = max(0, int(self._count(text)))
        if self.path is None:
            with self._lock:
                return self._charge(key, cost, room)
        try:
            with _file_lock(self.path.resolve()), _ledger_lock(self.path):
                self._load()
                return self._charge(key, cost, room, persist=True)
        except OSError:
            self._corrupt = True
            return None

    def _charge(self, key: str, cost: int, room: int | None, *, persist: bool = False) -> int | None:
        """Check-and-add under the caller's locks: a closed book and a charge that does not fit return
        ``None``; a key already delivered this turn returns ``0`` and is never added or written again."""
        if self._corrupt or self._stale:
            return None
        if key in self._spent:
            return 0
        if self.total + cost > self._cap(room):
            return None
        self._spent[key] = cost
        if persist:
            self._save()
        return cost

    def _load(self) -> None:
        """Merge the file into memory: another process sharing this path may have charged since. A file
        that exists but is not a readable, valid page cannot prove what was spent, so it closes the book
        rather than reopening it at zero. A different ``turn`` is another turn's page: it merges nothing,
        and only a page still showing the turn read at construction is ours to take over — once the file
        has moved to a turn we never saw, this ledger is stale and never writes again."""
        try:
            raw = self.path.read_text("utf-8")
        except FileNotFoundError:
            observed, spent = _ABSENT, {}
        except OSError:
            self._corrupt = True
            return
        else:
            try:
                book = _book(json.loads(raw))
            except ValueError:
                book = None
            if book is None:
                self._corrupt = True
                return
            observed, spent = book["turn"], book["spent"]
        if observed == self.turn_id:
            for key, cost in spent.items():
                self._spent[key] = max(self._spent.get(key, 0), cost)
        elif self._seen is not None and self._seen != observed:
            self._stale = True     # the page moved to a turn we never read — ours is over; do not rewrite
        if self._seen is None:
            self._seen = observed

    def _save(self) -> None:
        """Atomic like the session files: a temp file in the same folder, then ``os.replace``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"turn": self.turn_id, "total": self.total, "spent": self._spent},
                          ensure_ascii=False).encode("utf-8")
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
