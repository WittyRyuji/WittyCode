"""Local capability/cost profiles of the models Fusion can seat, fed by the public Artificial Analysis leaderboard.

Each profile is one small JSON file under ``<app data>/fusion-profiles/``, keyed by a fixed-length hash of the exact
page model id (``chatgpt:…``, ``api:<provider>:…``, ``devin:…``) and reasoning effort — a different effort of the
same model is a different record and is never mixed in; hashing keeps Windows paths short and cannot collide on
case-insensitive filesystems. A stored record is a cache hit: reading it never touches the network. On a miss the
injected ``fetch`` callable supplies the leaderboard fields; a failure keeps any earlier value and, with none,
stores a mid-low estimate with unknown cost so Fusion still has something to plan with instead of nothing. The
estimate is not permanent: after ``FALLBACK_RETRY_SECONDS`` the next ensure retries the source once, and a failed
retry only moves the throttle timestamp, keeping the record retryable rather than a forever-negative. Writes are
atomic and one file per key, so concurrent prefetch of different seats loses nothing.

``taskCostUSD`` is the leaderboard's USD per task and ``timeMinutes`` its minutes per task — the reference for
estimating what a *task* costs, not the price the user pays; ``costIn`` / ``costOut`` hold per-1M-token figures when
a source offers them. Pure: no Qt, no network of its own (``fetch`` is injected), no reads or writes outside
``root``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

FOLDER = "fusion-profiles"
SOURCE = "artificialanalysis"
"""The single public source these profiles are allowed to come from."""
FALLBACK_SCORE = 20.0
"""The mid-low capability estimate used when the leaderboard cannot say (offline, or the model is not listed)."""
FALLBACK_RETRY_SECONDS = 6 * 3600
"""How long a failed or absent listing is trusted before the next ensure tries the source one more time."""
CONFIDENCE_EXACT = "exact"
CONFIDENCE_FALLBACK = "fallback"

_NUMBERS = ("score", "costIn", "costOut", "taskCostUSD", "timeMinutes")

Fetch = Callable[[str, str], Mapping[str, Any] | None]
"""``fetch(model, effort)`` -> the source's fields for exactly that pair, or ``None`` when it has none. May raise."""

_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _check(model: Any, effort: Any) -> tuple[str, str]:
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError("The Fusion model profile needs a valid model id.")
    if not isinstance(effort, str) or len(effort) > 64:
        raise ValueError("The Fusion model profile needs a valid reasoning effort.")
    return model.strip(), effort.strip()


def _file(root: Path, model: str, effort: str) -> Path:
    """The record's own path: one fixed-length digest per (model, effort), short on Windows and free of case folds."""
    key = hashlib.sha256(f"{model}\x00{effort}".encode("utf-8")).hexdigest()
    return Path(root) / FOLDER / f"{key}.json"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _record(model: str, effort: str, offered: Mapping[str, Any], at: float) -> dict[str, Any]:
    return {"model": model, "effort": effort, "source": str(offered.get("source") or SOURCE),
            "score": _number(offered.get("score")), "costIn": _number(offered.get("costIn")),
            "costOut": _number(offered.get("costOut")), "taskCostUSD": _number(offered.get("taskCostUSD")),
            "timeMinutes": _number(offered.get("timeMinutes")), "fetchedAt": float(at),
            "confidence": CONFIDENCE_EXACT}


def _fallback(model: str, effort: str, at: float) -> dict[str, Any]:
    return {"model": model, "effort": effort, "source": CONFIDENCE_FALLBACK, "score": FALLBACK_SCORE,
            "costIn": None, "costOut": None, "taskCostUSD": None, "timeMinutes": None, "fetchedAt": float(at),
            "confidence": CONFIDENCE_FALLBACK}


def load(root: Path, model: str, effort: str) -> dict[str, Any] | None:
    """The stored record for exactly this (model, effort); ``None`` when absent, corrupt or malformed. Never fetches."""
    model, effort = _check(model, effort)
    try:
        record = json.loads(_file(root, model, effort).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("model") != model or record.get("effort") != effort
            or record.get("confidence") not in (CONFIDENCE_EXACT, CONFIDENCE_FALLBACK)):
        return None
    if _number(record.get("fetchedAt")) is None or any(
            record.get(field) is not None and _number(record.get(field)) is None for field in _NUMBERS):
        return None
    return record


def _write(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def ensure(root: Path, model: str, effort: str, fetch: Fetch, *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """The record for exactly this (model, effort), fetching and storing it only on a miss.

    A stored exact record — or a fallback younger than ``FALLBACK_RETRY_SECONDS`` — is returned as is and ``fetch``
    is not called, so a selection that repeats (reopening, another sign-in event, a turn ending) never multiplies
    into extra requests. An expired fallback gets one retry; a failed retry refreshes only the timestamp that
    throttles the next one. A first miss that cannot be answered is stored as a mid-low estimate with unknown cost,
    so the next call is a hit instead of another request.
    """
    model, effort = _check(model, effort)
    path = _file(root, model, effort)
    with _lock_for(path):
        record = load(root, model, effort)
        if record is not None and (
                record["confidence"] == CONFIDENCE_EXACT or clock() - record["fetchedAt"] < FALLBACK_RETRY_SECONDS):
            return record
        try:
            offered = fetch(model, effort)
        except Exception:
            offered = None
        if isinstance(offered, Mapping):
            record = _record(model, effort, offered, clock())
        elif record is None:
            record = _fallback(model, effort, clock())
        else:
            # The estimate stays an estimate — never a permanent negative — and the retry is throttled by fetchedAt.
            record = {**record, "fetchedAt": clock()}
        _write(path, record)
        return record


def _money(value: Any) -> str:
    return f"${value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def team_snapshot(root: Path, seats: dict[str, tuple[str, str]]) -> str:
    """A read-only view of the team's local model profiles for planning.

    ``seats`` maps a seat name (``main``, ``worker``, ``light``, …) to the (page model id, effort) sitting in it.
    Reads the stored records only: it never fetches, never writes and never touches anything outside ``root``, so a
    seat with no profile yet is reported as such rather than blocking the planner on the network.
    """
    lines = [f"Fusion team model profiles (local records; source: public {SOURCE} leaderboard):"]
    for seat, picked in seats.items():
        if not isinstance(picked, (tuple, list)) or len(picked) != 2:
            raise ValueError("The Fusion team snapshot needs (model, effort) seats.")
        model, effort = _check(picked[0], picked[1])
        effort_text = f" @ {effort}" if effort else ""
        record = load(root, model, effort)
        if record is None:
            lines.append(f"- {seat}: {model}{effort_text} — no local profile")
            continue
        score = record.get("score")
        capability = f"capability {score:g}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "capability unknown"
        parts = []
        if record.get("taskCostUSD") is not None:
            parts.append(f"{_money(record['taskCostUSD'])} per task")
        if record.get("costIn") is not None or record.get("costOut") is not None:
            parts.append(f"{_money(record.get('costIn'))}/{_money(record.get('costOut'))} per 1M tokens in/out")
        if record.get("timeMinutes") is not None:
            parts.append(f"~{record['timeMinutes']:g} min per task")
        cost = "task cost " + (", ".join(parts) if parts else "unknown")
        fetched = record.get("fetchedAt")
        day = time.strftime("%Y-%m-%d", time.gmtime(fetched)) if isinstance(fetched, (int, float)) else "unknown date"
        lines.append(f"- {seat}: {model}{effort_text} — {capability}, {cost}, confidence {record.get('confidence')}, fetched {day}")
    return "\n".join(lines)
