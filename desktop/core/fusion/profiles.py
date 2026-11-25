"""Local capability/cost profiles of the models Fusion can seat, fed by the public Artificial Analysis leaderboard.

Profiles share ``<app data>/fusion-profiles/models.json``, indexed by exact page model id and reasoning effort.
The old per-pair files are imported without fetching or changing them. Reads are local; on a miss the injected
``fetch`` supplies exact measurements or a Max capability reference, never Max cost or time for another effort.
A failed lookup keeps the previous reference or uses a mid-low estimate. Fetches do not hold the catalog write lock; each write
merges into the latest catalog and replaces the file atomically, including across app processes.

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

from desktop.core.fusion import planning
from desktop.core.plugin_lock import lock_file

FOLDER = "fusion-profiles"
FILE = "models.json"
SOURCE = "artificialanalysis"
"""The single public source these profiles are allowed to come from."""
FALLBACK_SCORE = 20.0
"""The mid-low capability estimate used when the leaderboard cannot say (offline, or the model is not listed)."""
FALLBACK_RETRY_SECONDS = 6 * 3600
"""How long a failed or absent listing is trusted before the next ensure tries the source one more time."""
CONFIDENCE_EXACT = "exact"
CONFIDENCE_MAX_REFERENCE = "max_reference"
CONFIDENCE_FALLBACK = "fallback"

_LOOKUP_VERSION = 2  # Old incomplete records should try the Max lookup once, even inside their retry window.
_NUMBERS = ("score", "costIn", "costOut", "taskCostUSD", "timeMinutes")

Fetch = Callable[[str, str], Mapping[str, Any] | None]
"""Source fields for the requested pair, optionally with separate evidence for a Max score, or ``None``. May raise."""

_locks_guard = threading.Lock()
_locks: dict[tuple[Path, str, str], threading.Lock] = {}


def _lock_for(path: Path, model: str, effort: str) -> threading.Lock:
    key = (path, model, effort)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _check(model: Any, effort: Any) -> tuple[str, str]:
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError("The Fusion model profile needs a valid model id.")
    if not isinstance(effort, str) or len(effort) > 64:
        raise ValueError("The Fusion model profile needs a valid reasoning effort.")
    return model.strip(), effort.strip()


def _legacy_name(model: str, effort: str) -> str:
    key = hashlib.sha256(f"{model}\x00{effort}".encode("utf-8")).hexdigest()
    return f"{key}.json"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _provenance(value: Any) -> dict[str, Any] | None:
    """The matched leaderboard row's evidence, kept only as the bounded dict the source was asked for: the page
    URL, the row's displayed label and tier text, and the raw cells the figures were read from. Anything else is
    dropped, not stored — an unknown provenance is absent, never made up."""
    if not isinstance(value, Mapping):
        return None
    url, label, tier, cells = value.get("url"), value.get("label"), value.get("tier"), value.get("cells")
    if (not isinstance(url, str) or len(url) > 512 or not isinstance(label, str) or len(label) > 256
            or not isinstance(tier, str) or len(tier) > 64 or not isinstance(cells, Mapping) or len(cells) > 8):
        return None
    if any(not isinstance(key, str) or len(key) > 64 or not isinstance(shown, str) or len(shown) > 256
           for key, shown in cells.items()):
        return None
    return {"url": url, "label": label, "tier": tier, "cells": dict(cells)}


def _record(model: str, effort: str, offered: Mapping[str, Any], at: float) -> dict[str, Any]:
    record = {"model": model, "effort": effort, "source": str(offered.get("source") or SOURCE),
              "score": _number(offered.get("score")), "costIn": _number(offered.get("costIn")),
              "costOut": _number(offered.get("costOut")), "taskCostUSD": _number(offered.get("taskCostUSD")),
              "timeMinutes": _number(offered.get("timeMinutes")), "fetchedAt": float(at),
              "confidence": CONFIDENCE_EXACT, "lookupVersion": _LOOKUP_VERSION}
    provenance = _provenance(offered.get("provenance"))
    if provenance is not None:
        record["provenance"] = provenance
    score_provenance = _provenance(offered.get("scoreProvenance"))
    if record["score"] is not None and score_provenance is not None and score_provenance["tier"].casefold() == "max":
        record["confidence"] = CONFIDENCE_MAX_REFERENCE
        record["scoreProvenance"] = score_provenance
    return record


def _fallback(model: str, effort: str, at: float) -> dict[str, Any]:
    return {"model": model, "effort": effort, "source": CONFIDENCE_FALLBACK, "score": FALLBACK_SCORE,
            "costIn": None, "costOut": None, "taskCostUSD": None, "timeMinutes": None, "fetchedAt": float(at),
            "confidence": CONFIDENCE_FALLBACK, "lookupVersion": _LOOKUP_VERSION}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None  # Other I/O errors must not be mistaken for an empty catalog by a writer.


def _validated(record: Any, model: str, effort: str) -> dict[str, Any] | None:
    if (not isinstance(record, dict) or record.get("model") != model or record.get("effort") != effort
            or record.get("confidence") not in (CONFIDENCE_EXACT, CONFIDENCE_MAX_REFERENCE, CONFIDENCE_FALLBACK)):
        return None
    if _number(record.get("fetchedAt")) is None or any(
            record.get(field) is not None and _number(record.get(field)) is None for field in _NUMBERS):
        return None
    if record["confidence"] == CONFIDENCE_MAX_REFERENCE:
        evidence = _provenance(record.get("scoreProvenance"))
        if record.get("score") is None or evidence is None or evidence["tier"].casefold() != "max":
            return None
        record["scoreProvenance"] = evidence
    if "provenance" in record:
        provenance = _provenance(record["provenance"])
        if provenance is None:
            del record["provenance"]  # Corrupted evidence is dropped, never served as the row's.
        else:
            record["provenance"] = provenance
    return record


def _read_catalog(path: Path) -> dict[str, Any] | None:
    data = _read_json(path)
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else None


def _find(models: dict[str, Any], model: str, effort: str) -> dict[str, Any] | None:
    by_effort = models.get(model)
    return _validated(by_effort.get(effort), model, effort) if isinstance(by_effort, dict) else None


def load(root: Path, model: str, effort: str) -> dict[str, Any] | None:
    """Read an exact pair without fetching or writing, even while startup is still migrating the old files."""
    model, effort = _check(model, effort)
    folder = Path(root) / FOLDER
    try:
        models = _read_catalog(folder / FILE)
        if models is not None:
            return _find(models, model, effort)
        return _validated(_read_json(folder / _legacy_name(model, effort)), model, effort)
    except OSError:
        return None


def _legacy_catalog(folder: Path) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for path in folder.glob("*.json"):
        if (len(path.stem) != 64 or any(c not in "0123456789abcdef" for c in path.stem)
                or path.is_symlink() or not path.is_file()):
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        try:
            model, effort = _check(data.get("model"), data.get("effort"))
            if path.name != _legacy_name(model, effort):
                continue
        except ValueError:
            continue
        record = _validated(data, model, effort)
        if record is not None:
            models.setdefault(model, {})[effort] = record
    return models


def _write(path: Path, models: Mapping[str, Any]) -> None:
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"models": models}, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        for attempt in range(10):
            try:
                os.replace(temp, path)
                break
            except PermissionError as error:
                # A lock-free reader on Windows can briefly prevent the atomic replacement.
                if getattr(error, "winerror", None) not in (5, 32) or attempt == 9:
                    raise
                time.sleep(0.025)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _catalog_lock(path: Path):
    return lock_file(path, busy_message="Fusion 模型评分档案正在使用中，请稍后重试。")


def _import_locked(path: Path) -> dict[str, Any]:
    models = _read_catalog(path)
    if models is None:
        models = _legacy_catalog(path.parent)
        if models:
            _write(path, models)
    return models


def _catalog_after_migration(path: Path) -> dict[str, Any]:
    models = _read_catalog(path)
    if models is not None:
        return models
    if not path.parent.is_dir():
        return {}
    with _catalog_lock(path):
        return _import_locked(path)


def migrate(root: Path) -> None:
    """Import old profiles once if there is no readable catalog yet. No fetch and no removal of old records."""
    _catalog_after_migration(Path(root) / FOLDER / FILE)


def _exact_score(record: dict[str, Any]) -> bool:
    return record["confidence"] == CONFIDENCE_EXACT and record.get("score") is not None


def _current(record: dict[str, Any], now: float) -> bool:
    return _exact_score(record) or (record.get("lookupVersion") == _LOOKUP_VERSION
                                    and now - record["fetchedAt"] < FALLBACK_RETRY_SECONDS)


def ensure(root: Path, model: str, effort: str, fetch: Fetch, *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Keep exact scores. Recheck incomplete scores and references after the retry window or a lookup-rule change."""
    model, effort = _check(model, effort)
    path = Path(root) / FOLDER / FILE
    with _lock_for(path.resolve(), model, effort):
        record = _find(_catalog_after_migration(path), model, effort)
        if record is not None and _current(record, clock()):
            return record
        try:
            offered = fetch(model, effort)
        except Exception:
            offered = None
        with _catalog_lock(path):
            models = _import_locked(path)
            record = _find(models, model, effort)  # Another process may have written while we were fetching.
            if record is not None and _exact_score(record):
                return record
            now = clock()
            if isinstance(offered, Mapping):
                updated = _record(model, effort, offered, now)
                if (updated["score"] is None and record is not None
                        and record["confidence"] == CONFIDENCE_MAX_REFERENCE):
                    for key in ("score", "scoreProvenance", "confidence"):
                        updated[key] = record[key]
                record = updated
            elif record is None:
                record = _fallback(model, effort, now)
            elif _current(record, now):
                return record
            else:
                record = {**record, "fetchedAt": now, "lookupVersion": _LOOKUP_VERSION}  # Throttle a failed retry.
            if not isinstance(models.get(model), dict):
                models[model] = {}
            models[model][effort] = record
            _write(path, models)
            return record


def _money(value: Any) -> str:
    return f"${value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def team_snapshot(root: Path, seats: dict[str, tuple[str, str]]) -> str:
    """A read-only view of the team's local model profiles plus the V3 planning contract.

    ``seats`` maps a seat name (``main``, ``worker``, ``light``, …) to the (page model id, effort) sitting in it.
    Reads the stored records only: it never fetches, never writes and never touches anything outside ``root``, so a
    seat with no profile yet is reported as such rather than blocking the planner on the network. Each hit keeps
    the public source's fetch date, the matched (model, effort) identifier, capability, task cost and speed;
    values the record does not know stay unknown instead of being made up.

    The seat lines are followed by ``planning.CONTRACT`` — the internal planning data structure and the rules a
    planning turn submits to the harness — because this snapshot is only ever assembled for planning.
    ``planning.parse`` is the standalone validator the calling side runs the planning JSON through.
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
        if record["confidence"] == CONFIDENCE_MAX_REFERENCE:
            capability += " (Max capability ceiling; not measured at this effort)"
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
    return "\n".join(lines) + "\n\n" + planning.CONTRACT
