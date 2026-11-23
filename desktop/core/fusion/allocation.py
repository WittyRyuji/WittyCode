"""Frozen work-item allocations: which of the three policy seats a workstream dispatches to.

A route declares items once; the registry then keeps them frozen for the rest of the session.
``seal`` normalizes one item into its stored record and recomputes its seat from (N, score)
with :func:`dispatch.tier`, so a stored or re-submitted allocation can never disagree with
its inputs — re-scoring or renaming is refused at freeze time and a tampered record at load
time. Pure helpers: no I/O and no knowledge of the registry or the models themselves.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

from desktop.core.fusion.dispatch import check_index, check_score, tier
from desktop.core.fusion.identity import WORK_ITEM_ROLES

SEATS = ("L1", "L2", "L3")
SEAT_ROLE = {"L1": "main", "L2": "worker", "L3": "light"}
"""Which policy reasoning level a seat uses."""
_ROLES = ("worker", "light")
_ITEM_KEYS = frozenset(("id", "workstream", "role", "score", "reason", "paths", "deps", "acceptance"))
_MAX_ID = 64
_MAX_DEPS = 16
_MAX_PATHS = 128


def key(workstream: str, role: str) -> tuple[str, str]:
    """The binding key: same workstream and expert role share one frozen allocation."""
    return workstream.strip().casefold(), role


def _text(value: Any, field_name: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"A work item needs a valid {field_name}.")
    return value.strip()


def _paths(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_PATHS or any(
            not isinstance(path, str) or not path.strip() or any(c in path for c in "*?\0") for path in value):
        raise ValueError("A work item's paths must be a list of concrete project-relative paths.")
    return tuple(dict.fromkeys(path.strip().replace("\\", "/") for path in value))


def _deps(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_DEPS or any(
            not isinstance(dep, str) or not dep.strip() or len(dep) > _MAX_ID for dep in value):
        raise ValueError("A work item's deps must be a short list of other item ids.")
    return tuple(dep.strip() for dep in value)


def _decimal(value: Any) -> Decimal:
    """The score as an exact finite ``Decimal`` for storage; a non-terminating Fraction is refused."""
    score = check_score(value)
    text = format(Decimal(score.numerator) / Decimal(score.denominator), "f")
    if check_score(text) != score:
        raise ValueError("A work item's score must be an exact decimal in 0..1.")
    return Decimal(text)


def seal(raw: Mapping[str, Any], N: object, *, from_route: bool = False) -> dict[str, Any]:
    """Validate an allocation under the session's preference, reporting all invalid fields together.

    ``N`` comes from the runtime, not the route. Stored ``N`` and ``seat`` must match the recomputed values.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("A work item must be an object.")
    problems: list[str] = []

    def checked(validate: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return validate(*args, **kwargs)
        except ValueError as error:
            problems.append(str(error))
            return None

    unknown = set(raw) - (_ITEM_KEYS | {"N", "seat", "new_facts"})
    score_alias = bool(unknown & {"n", "n_i", "difficulty"})
    if unknown:
        problems.append(f"Unknown work item fields: {', '.join(sorted(map(str, unknown)))}.")
    if score_alias:
        problems.append("Use score (decimal in 0..1) for difficulty; omit the aliases.")
    if raw.get("score") is None and not score_alias:
        problems.append("A work item needs a decimal score in 0..1.")
    score = checked(_decimal, raw["score"]) if raw.get("score") is not None else None
    role = raw.get("role")
    if role not in _ROLES:
        problems.append(f"Set role to {WORK_ITEM_ROLES}.")
    index = checked(check_index, N)
    if "N" in raw and (from_route or (index is not None and raw["N"] != index)):
        problems.append("Omit N: it is fixed by the session preference, not by the brief.")
    record = {
        "id": checked(_text, raw.get("id"), "id", _MAX_ID),
        "workstream": checked(_text, raw.get("workstream"), "workstream", 160),
        "role": role,
        "N": index,
        "score": format(score, "f") if score is not None else None,
        "reason": checked(_text, raw.get("reason"), "reason", 4000),
        "paths": checked(_paths, raw.get("paths")),
        "deps": checked(_deps, raw.get("deps")),
        "acceptance": checked(_text, raw.get("acceptance", ""), "acceptance", 50_000, required=False),
    }
    if index is not None and score is not None:
        record["seat"] = tier(index, check_score(record["score"]))
        if "seat" in raw and raw["seat"] != record["seat"]:
            problems.append("A stored work item seat does not match its N and score.")
    if raw.get("new_facts") is not None:
        record["new_facts"] = checked(_text, raw["new_facts"], "new_facts", 4000)
    if problems:
        raise ValueError(" ".join(problems))
    return record


def _score_of(record: Mapping[str, Any]):
    return check_score(record.get("score"))


def same(frozen: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Two allocations match when every model-supplied field agrees; score compares exactly, not textually.
    ``N``, ``seat`` and ``new_facts`` are runtime-derived or request-only and never part of the comparison,
    so an identical item re-sealed under a moved preference still matches its frozen record."""
    if not isinstance(candidate, Mapping):
        return False
    try:
        other = seal({name: candidate[name] for name in _ITEM_KEYS if name in candidate}, frozen["N"])
    except ValueError:
        return False
    fields = _ITEM_KEYS - {"score"}
    return all(frozen.get(field) == other[field] for field in fields) and _score_of(frozen) == _score_of(other)


def reestimate(frozen: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """``candidate`` only re-estimates ``frozen`` when every field but ``score`` and ``reason`` is unchanged:
    a new-facts update may move the estimate, never the item's identity, write scope or dependencies."""
    fields = _ITEM_KEYS - {"score", "reason"}
    return all(frozen.get(field) == candidate.get(field) for field in fields)


def covers(record: Mapping[str, Any], given: Sequence[str]) -> bool:
    """Delegated write paths must stay inside the frozen ones. An empty frozen list claims none, and a
    delegation that declares no paths reserves the project — inside nothing that was frozen."""
    frozen = record.get("paths") or ()
    if not given or not frozen:
        return False
    return all(any(given_path == path or given_path.startswith(path + "/") for path in frozen) for given_path in given)


def check_deps(records: Sequence[Mapping[str, Any]]) -> str:
    """Deps must point at items frozen in the same session and form no cycles, including across prior freezes."""
    known = {record["id"] for record in records}
    for record in records:
        dangling = [dep for dep in record["deps"] if dep not in known]
        if dangling:
            return f"Work item {record['id']!r} depends on unknown ids: {', '.join(dangling)}."
        if record["id"] in record["deps"]:
            return f"Work item {record['id']!r} depends on itself."
    deps = {record["id"]: record["deps"] for record in records}
    done: set[str] = set()
    for record in records:  # Iterative DFS: a dependency chain can outgrow the recursion limit.
        visiting = {record["id"]}
        stack = [(record["id"], iter(deps.get(record["id"], ())))]
        while stack:
            node, edges = stack[-1]
            for dep in edges:
                if dep in done:
                    continue
                if dep in visiting:
                    return "Work item dependencies form a cycle."
                visiting.add(dep)
                stack.append((dep, iter(deps.get(dep, ()))))
                break
            else:
                stack.pop()
                visiting.discard(node)
                done.add(node)
    return ""
