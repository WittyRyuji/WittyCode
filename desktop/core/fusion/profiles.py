"""Fusion's local model references: one score and USD per benchmark task per page model ID.

Reads never fetch. Writes merge with the latest catalog under a cross-process lock; the public
leaderboard lookup is injected and runs outside that lock.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from desktop.core.fusion import planning
from desktop.core.plugin_lock import lock_file

FOLDER = "fusion-profiles"
FILE = "models.json"
_FIELDS = ("score", "cost")

Profile = dict[str, float | None]
Fetch = Callable[[str], Mapping[str, Any] | None]

_locks_guard = threading.Lock()
_locks: dict[tuple[Path, str], threading.Lock] = {}


def _lock_for(path: Path, model: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((path, model), threading.Lock())


def _check(model: Any) -> str:
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError("The Fusion model profile needs a valid model id.")
    return model.strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _compact(record: Any) -> Profile:
    return {key: _number(record.get(key)) if isinstance(record, Mapping) else None for key in _FIELDS}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None  # An I/O error is not an empty catalog and must not cause a writer to replace it.


def _collapse(model: str, efforts: dict) -> Profile:
    measurements = []
    for effort, record in efforts.items():
        if not isinstance(record, dict) or record.get("model") != model or record.get("effort") != effort:
            continue
        level = effort.casefold()
        if record.get("confidence") == "exact":
            measurements.append((0 if level == "max" else 2, _number(record.get("score")), _number(record.get("taskCostUSD"))))
        elif record.get("confidence") == "max_reference":
            # In the old format the Max-derived score could be paired with a different effort's cost.
            measurements.append((1, _number(record.get("score")), None))
    scored = [(rank, score, cost) for rank, score, cost in measurements if score is not None]
    if scored:
        rank = min(row[0] for row in scored)
        score = max(row[1] for row in scored if row[0] == rank)
        costs = {cost for order, points, cost in scored if order == rank and points == score and cost is not None}
    else:
        score = None
        costs = {cost for _, _, cost in measurements if cost is not None}
    return {"score": score, "cost": next(iter(costs)) if len(costs) == 1 else None}


def _legacy(folder: Path) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for path in folder.glob("*.json"):
        if (len(path.stem) != 64 or any(c not in "0123456789abcdef" for c in path.stem)
                or path.is_symlink() or not path.is_file()):
            continue
        record = _read_json(path)
        if not isinstance(record, dict):
            continue
        try:
            model, effort = _check(record.get("model")), record.get("effort")
            if not isinstance(effort, str) or len(effort) > 64:
                continue
            digest = hashlib.sha256(f"{model}\x00{effort}".encode("utf-8")).hexdigest()
        except ValueError:
            continue
        if digest == path.stem and record.get("confidence") in ("exact", "max_reference", "fallback"):
            models.setdefault(model, {})[effort] = record
    return models


def _read(path: Path) -> tuple[dict[str, Profile], bool]:
    data = _read_json(path)
    stored = data.get("models") if isinstance(data, dict) else None
    rows = stored if isinstance(stored, dict) else _legacy(path.parent)
    models = {}
    for model, row in rows.items():
        if not isinstance(row, dict) or not model.strip() or len(model) > 512:
            continue
        models[model] = _compact(row) if set(row) == set(_FIELDS) else _collapse(model, row)
    return models, models != rows


def _write(path: Path, models: Mapping[str, Profile]) -> None:
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


def _request(fetch: Fetch, model: str) -> Profile:
    try:
        return _compact(fetch(model))
    except Exception:
        return _compact(None)


def _save(path: Path, updates: Mapping[str, Profile]) -> dict[str, Profile]:
    with _catalog_lock(path):
        models, changed = _read(path)
        for model, offered in updates.items():
            prior = models.get(model)
            # Don't replace a measured score with a cost-only lookup or a transient miss. Keep both numbers
            # from the same reference; a new score must not inherit the cost of an unrelated leaderboard row.
            use_offer = (prior is None or offered["score"] is not None
                         or (prior["score"] is None and offered["cost"] is not None))
            if use_offer and offered != prior:
                models[model] = offered
                changed = True
        if changed:
            _write(path, models)
        return models


def refresh(root: Path, selected: Iterable[str], fetch: Fetch) -> None:
    """Upgrade old files and refresh saved/configured models in one startup batch, without model calls."""
    path = Path(root) / FOLDER / FILE
    models, changed = _read(path)
    keys = dict.fromkeys([*models, *(_check(model) for model in selected)])
    if keys or changed:
        _save(path, {model: _request(fetch, model) for model in keys})


def load(root: Path, model: str) -> Profile | None:
    """Read an exact model ID without fetching or writing, even before startup migration completes."""
    model = _check(model)
    try:
        return _read(Path(root) / FOLDER / FILE)[0].get(model)
    except OSError:
        return None


def ensure(root: Path, model: str, fetch: Fetch) -> Profile:
    """Use a complete local record or look up the model; the source throttles public-page downloads."""
    model = _check(model)
    path = Path(root) / FOLDER / FILE
    # A key must not change while the target file is being created or replaced on Windows.
    with _lock_for(Path(os.path.abspath(path)), model):
        models, changed = _read(path)
        record = models.get(model)
        if not changed and record is not None and all(value is not None for value in record.values()):
            return record
        return _save(path, {model: _request(fetch, model)})[model]


def team_snapshot(root: Path, seats: dict[str, tuple[str, str]]) -> str:
    """Use local model-level references. The seat's actual effort is policy, not part of its profile."""
    try:
        models, _ = _read(Path(root) / FOLDER / FILE)
    except OSError:
        models = {}
    lines = ["Fusion team model profiles (Artificial Analysis benchmark; not a measurement at the seat's effort or a billing quote):"]
    for seat, picked in seats.items():
        if not isinstance(picked, (tuple, list)) or len(picked) != 2:
            raise ValueError("The Fusion team snapshot needs (model, effort) seats.")
        model, effort = _check(picked[0]), picked[1]
        effort_text = f" @ {effort}" if effort else ""
        record = models.get(model) or _compact(None)
        score, cost = record["score"], record["cost"]
        capability = f"capability {score:g}" if score is not None else "capability unknown"
        cost_text = f"${cost:g} per benchmark task" if cost is not None else "unknown"
        lines.append(f"- {seat}: {model}{effort_text} — {capability}, task cost {cost_text}")
    return "\n".join(lines) + "\n\n" + planning.CONTRACT
