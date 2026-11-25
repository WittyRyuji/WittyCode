"""The V3 planning facts contract: the one JSON structure the main agent hands the harness for a planning turn.

``profiles.team_snapshot`` appends :data:`CONTRACT` to the seat lines it hands the main agent — only ever in the
planning phase — so the model sees the team's local capability/cost records and the shape its plan must take in
the same text. The JSON is internal planning data submitted to the harness; the answer the user sees stays prose,
it is not required to be this object. ``check`` validates a decoded plan object into its normalized form;
``parse`` does the ``json.loads`` first and returns a refusal string instead of raising, which is what a tool
handler needs.

The contract's invariants, all enforced here:

- ``delivery`` is ``"direct"`` (delivered whole — by the main agent itself, or its single item frozen for one
  Coder) or ``"candidates"`` (split into at least two independently-acceptable workstreams). ``reason`` says why
  the choice fits.
- ``files`` is the deduplicated file count the plan expects to touch, or ``null`` when that is not known yet. It
  is an estimate, not a census: a declared directory covers several files and files not yet known are legitimate,
  so only a count below the declared concrete paths — the one clearly impossible answer — is refused.
- ``facts`` and ``uncertainties`` keep what local, read-only inspection established apart from what is still
  unknown; ``decisions`` names the core calls the plan hinges on and ``acceptance`` how the whole plan is
  checked.
- Each item carries its score estimate ``n`` — the same 0..1 decimal a frozen work item scores by, kept exact —
  with the ``evidence`` for it, the ``deps`` it must wait for and an optional per-item ``acceptance`` note. Deps
  must point at items in the same plan and form no cycles — the same rule the frozen allocations live by.
- ``budget`` keeps tokens, turns and seconds as three separate requests: ``"adaptive"`` leaves the sizing to
  Fusion, a number is a request the configured hard limits still clamp. The actual caps and consumption are
  unknown at plan time: a plan cannot promise to bound what the models will spend and never states consumption
  as fact.
- The session's preference ``N`` is the user's own slider, not a difficulty the plan may set: ``N`` or
  ``preference`` in a plan is refused outright, not clamped.

Pure: no I/O, no Qt, no knowledge of the models; every validator here is local arithmetic on the given object.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from desktop.core.fusion.allocation import check_deps
from desktop.core.fusion.dispatch import check_score

DELIVERIES = ("direct", "candidates")
"""Direct delivery by one owner versus a split into candidate workstreams."""
MIN_CANDIDATES = 2
"""A split into fewer than two workstreams is direct delivery, not a split."""
BUDGET_FIELDS = ("tokens", "turns", "seconds")
"""The three budget dimensions a plan may request separately, each ``"adaptive"`` or a positive integer."""
ADAPTIVE = "adaptive"

CONTRACT = """\
V3 planning data — one JSON object submitted to the harness as this turn's planning record; the answer the user
sees stays prose, it is not required to be this JSON. Validate before relying on it:
desktop.core.fusion.planning.parse.
{"delivery":"direct"|"candidates", "reason":"why this delivery fits", "files":<estimated deduplicated
 files>|null, "facts":["verified facts"], "uncertainties":["still unknown"], "decisions":["core decisions"],
 "acceptance":"how the whole plan is checked",
 "items":[{"id":"stable id", "workstream":"the deliverable", "paths":["project/relative"],
           "n":<0..1>, "evidence":"why this n_i", "deps":["id"], "acceptance":"item check"}],
 "budget":{"tokens":"adaptive"|int, "turns":"adaptive"|int, "seconds":"adaptive"|int}}
"direct" carries at most one item — none when you deliver it yourself, one when the whole deliverable freezes
for a single Coder; "candidates" needs two or more, each with its score estimate n (0..1, the same scale a
work item's score dispatches on) and the evidence for it. files is the deduplicated file count you expect to
touch — a declared directory covers several files and files not yet known are legitimate, so only a count below
the declared paths is refused; null when you cannot estimate. deps are item ids that must finish first. Budgets
keep tokens, turns and seconds as separate requests: "adaptive" leaves sizing to Fusion, a number is a request
the configured hard limits still clamp. The actual caps and consumption are unknown at plan time — a plan cannot
promise to bound what the models will spend and never states consumption as fact. The session's preference N is
the user's own slider, never a plan field. Facts come only from the local records above and read-only
inspection; what is unknown stays unknown."""
"""The planning section ``profiles.team_snapshot`` appends to the team lines."""

_MAX_ITEMS = 16
_MAX_ID = 64
_MAX_DEPS = 16
_MAX_PATHS = 128
_MAX_WORKSTREAM = 160
_MAX_REASON = 4000
_MAX_EVIDENCE = 4000
_MAX_ACCEPTANCE = 50_000
_MAX_LINES = 64
_MAX_LINE = 2000

_PLAN_KEYS = frozenset(("delivery", "reason", "files", "facts", "uncertainties", "decisions", "acceptance",
                        "items", "budget"))
_ITEM_KEYS = frozenset(("id", "workstream", "paths", "n", "evidence", "deps", "acceptance"))


def _text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or any(
            ord(c) < 32 for c in value):
        raise ValueError(f"A Fusion plan needs a valid {field}.")
    return value.strip()


def _lines(value: Any, field: str) -> tuple[str, ...]:
    """A bounded list of non-empty one-line strings; the field itself is required, its list may be empty."""
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_LINES or any(
            not isinstance(line, str) or not line.strip() or len(line) > _MAX_LINE
            or any(ord(c) < 32 for c in line) for line in value):
        raise ValueError(f"A Fusion plan's {field} must be a short list of non-empty strings.")
    return tuple(line.strip() for line in value)


def _paths(value: Any) -> tuple[str, ...]:
    """Concrete project-relative paths, normalized and deduplicated like the frozen write scope — the count
    ``files`` claims is only meaningful against the same normalization."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_PATHS or any(
            not isinstance(path, str) or not path.strip() or any(c in path for c in "*?\0") for path in value):
        raise ValueError("A plan item's paths must be a list of concrete project-relative paths.")
    return tuple(dict.fromkeys(path.strip().replace("\\", "/") for path in value))


def _deps(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_DEPS or any(
            not isinstance(dep, str) or not dep.strip() or len(dep) > _MAX_ID for dep in value):
        raise ValueError("A plan item's deps must be a short list of other item ids.")
    return tuple(dep.strip() for dep in value)


def _score(value: Any) -> str:
    """The item's ``n`` as its exact decimal string in 0..1 — the same scale a frozen work item's score
    dispatches on, stored as JSON data (never the internal ``Fraction``); a score no exact decimal names is
    refused like a stored allocation's would be."""
    score = check_score(value)
    text = format(Decimal(score.numerator) / Decimal(score.denominator), "f")
    if check_score(text) != score:
        raise ValueError("A plan item's n must be an exact decimal in 0..1.")
    return text


def _item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("A Fusion plan item must be an object.")
    unknown = set(raw) - _ITEM_KEYS
    if unknown:
        raise ValueError(f"Unknown plan item fields: {', '.join(sorted(map(str, unknown)))}.")
    return {
        "id": _text(raw.get("id"), "item id", _MAX_ID),
        "workstream": _text(raw.get("workstream"), "workstream", _MAX_WORKSTREAM),
        "paths": _paths(raw.get("paths")),
        "n": _score(raw.get("n")),
        "evidence": _text(raw.get("evidence"), "item evidence", _MAX_EVIDENCE),
        "deps": _deps(raw.get("deps")),
        "acceptance": _text(raw.get("acceptance", ""), "item acceptance", _MAX_ACCEPTANCE, required=False),
    }


def _budget(value: Any) -> dict[str, Any]:
    """The three requested budget dimensions; each defaults to ``"adaptive"`` — Fusion sizes it — and a number
    is only ever a request the hard limits clamp, so it must be a plain positive integer."""
    if not isinstance(value, Mapping):
        raise ValueError("A Fusion plan's budget must be an object of tokens, turns and seconds.")
    unknown = set(value) - set(BUDGET_FIELDS)
    if unknown:
        raise ValueError(f"Unknown plan budget fields: {', '.join(sorted(map(str, unknown)))}.")
    budget = {}
    for field in BUDGET_FIELDS:
        side = value.get(field, ADAPTIVE)
        if side != ADAPTIVE and (isinstance(side, bool) or not isinstance(side, int) or side < 1):
            raise ValueError(f"A Fusion plan's {field} budget must be 'adaptive' or a positive integer.")
        budget[field] = side
    return budget


def check(plan: object) -> dict[str, Any]:
    """Validate one decoded V3 planning object into its normalized form.

    Raises ``ValueError`` with the broken rule; the caller that must answer the model uses :func:`parse`, which
    returns the same message as a refusal string.
    """
    if not isinstance(plan, Mapping):
        raise ValueError("A Fusion plan must be a JSON object.")
    unknown = set(plan) - _PLAN_KEYS
    if unknown & {"N", "preference"}:
        raise ValueError("The session's preference N is the user's own slider, never a plan field.")
    if unknown:
        raise ValueError(f"Unknown plan fields: {', '.join(sorted(map(str, unknown)))}.")
    delivery = plan.get("delivery")
    if delivery not in DELIVERIES:
        raise ValueError("A Fusion plan's delivery must be 'direct' or 'candidates'.")
    files = plan.get("files")
    if files is not None and (isinstance(files, bool) or not isinstance(files, int) or files < 0):
        raise ValueError("A Fusion plan's files must be the estimated deduplicated file count or null.")
    raw_items = plan.get("items")
    if raw_items is not None and (not isinstance(raw_items, (list, tuple)) or len(raw_items) > _MAX_ITEMS):
        raise ValueError("A Fusion plan's items must be a list of candidate workstreams.")
    items = [_item(raw) for raw in (raw_items or ())]
    if delivery == "direct":
        if len(items) > 1:
            raise ValueError("A direct Fusion plan carries at most one item.")
    elif len(items) < MIN_CANDIDATES:
        raise ValueError("A candidates Fusion plan splits into at least two items.")
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("A Fusion plan's item ids must be unique.")
    declared = len({path for item in items for path in item["paths"]})
    if files is not None and files < declared:
        raise ValueError(
            f"A Fusion plan's files is {files}, below the {declared} declared concrete paths.")
    message = check_deps([{"id": item["id"], "deps": item["deps"]} for item in items])
    if message:
        raise ValueError(message)
    return {
        "delivery": delivery,
        "reason": _text(plan.get("reason"), "reason", _MAX_REASON),
        "files": files,
        "facts": _lines(plan.get("facts"), "facts"),
        "uncertainties": _lines(plan.get("uncertainties"), "uncertainties"),
        "decisions": _lines(plan.get("decisions"), "decisions"),
        "acceptance": _text(plan.get("acceptance"), "acceptance", _MAX_ACCEPTANCE),
        "items": items,
        "budget": _budget(plan.get("budget")),
    }


UNIT_CONTRACT = (
    'submit_work_plan(data={"units":[{"id":"a","task":"deliverable","J":5,"I":4,"C":2,'
    '"basis":"observed facts","paths":["src"],"acceptance":"check"}]}) once. '
    'J=judgment, I=importance, C=coordination (integers 0..10). Optional deps, expert or tags (author), '
    'work_type (execute/manage/research/verify/review), review_required/risk/force_lead (hard signals). '
    'Optional data.budget {tokens,turns,seconds} caps the whole Plan; Fusion computes unit steps. '
    'Use [] for Main-only delivery. Do not select a model. After approval or submit_route({}), '
    'delegate_task({"unit":"a"}) advances the frozen unit. Read the receipt for its next step.'
)


def unit_facts(data: Mapping[str, Any]) -> tuple[list[dict], dict]:
    """Untrusted tool boundary. Never accept derived fields or a model-supplied roster."""
    if set(data) - {"units", "budget"}:
        raise ValueError("Submit only units and an optional budget, not selection results.")
    raw = data.get("units")
    if not isinstance(raw, list) or len(raw) > _MAX_ITEMS:
        raise ValueError("Submit a short list of units with J/I/C facts.")
    units = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {
                "id", "task", "J", "I", "C", "basis", "acceptance", "paths", "deps", "expert", "tags", "work_type",
                "risk", "review_required", "force_lead"}:
            raise ValueError("Unknown unit field; supply facts, not a model, seat, budget or preference.")
        facts = {key: item.get(key) for key in ("J", "I", "C")}
        if any(type(v) is not int or not 0 <= v <= 10 for v in facts.values()):
            raise ValueError("Each unit needs integer J, I and C in 0..10.")
        signals = {key: item.get(key, False) for key in ("review_required", "risk", "force_lead")}
        if any(type(v) is not bool for v in signals.values()):
            raise ValueError("Hard review signals must be booleans.")
        work_type = item.get('work_type', 'execute')
        if work_type not in ('execute', 'manage', 'research', 'verify', 'review'):
            raise ValueError('Unknown author work type (observe is reserved for the harness).')
        units.append({"work_type": work_type, "id": _text(item.get("id"), "unit id", _MAX_ID),
                      "task": _text(item.get("task"), "task", 2000), "basis": _text(item.get("basis"), "basis", 1000),
                      "acceptance": _text(item.get("acceptance"), "acceptance", 2000), "paths": list(_paths(item.get("paths"))),
                      "deps": list(_deps(item.get("deps"))), "expert": _text(item.get("expert", ""), "expert", 128, required=False),
                      "tags": list(_lines(item.get("tags", []), "tags")), **facts, **signals})
    if len({u['id'].casefold() for u in units}) != len(units):
        raise ValueError("Each unit needs a unique id.")
    if problem := check_deps(units):
        raise ValueError(problem)
    return units, _budget(data.get("budget", {}))


def parse(text: object) -> tuple[dict[str, Any] | None, str]:
    """Decode and validate one planning JSON text for the calling side: ``(plan, "")`` on success,
    ``(None, reason)`` to hand back as the refusal."""
    if not isinstance(text, str):
        return None, "A Fusion plan must be a JSON text."
    try:
        return check(json.loads(text)), ""
    except ValueError as error:  # JSONDecodeError is a ValueError too: malformed text and a broken rule land here.
        return None, str(error)
