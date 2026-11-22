"""Managed-data budget: one global cap split into fixed per-category shares.

The Storage page's Max is a cap on application-managed data only. A category's budget is
``max_bytes * share / 100``; the shares must total exactly 100 and a category can never borrow
another's headroom. The alert threshold is a separate percent of the global cap, not part of the
split. When a write does not fit, the policy is ``stop`` (refuse; the user deals with it) or
``autoclean`` (the caller may drop the oldest eligible objects first — only in the categories the
user authorised, which start as cache and the rebuildable index; complete history and dumps are
never cleaned unless each is explicitly switched on). Unknown usage is never treated as zero:
anything the application does not itself measure fails closed.

Budgets are floored, so they may sum to less than ``max_bytes`` — never more.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

CATEGORIES = ("history", "dump", "cache", "index")
"""Managed-data classes: complete history, dumps, caches, and the rebuildable index."""
CATEGORY_LABELS = {"history": "完整历史", "dump": "转储", "cache": "缓存", "index": "可重建索引"}
DEFAULT_SHARES = {"history": 60.0, "dump": 15.0, "cache": 15.0, "index": 10.0}
DEFAULT_MAX_BYTES = 8 * 1024**3
MAX_BYTES_RANGE = (256 * 1024**2, 2**53 - 1)
DEFAULT_ALERT_AT = 80
ALERT_RANGE = (10, 100)
FULL_POLICIES = ("stop", "autoclean")
DEFAULT_AUTOCLEAN = ("cache", "index")
"""The categories auto-clean may touch before the user authorises history or dumps by name."""


@dataclass(frozen=True)
class StoragePolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    shares: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SHARES))
    alert_at: int = DEFAULT_ALERT_AT
    full_policy: str = "stop"
    autoclean: tuple[str, ...] = DEFAULT_AUTOCLEAN

    def budget(self, category: str) -> int:
        return int(self.max_bytes * self.shares.get(category, 0.0) / 100)


def shares_from(raw: object) -> dict[str, float] | None:
    """A complete split of the cap: every category once, nonnegative numbers, summing to 100.

    ``None`` when the shape or the total is off — a caller then refuses the edit rather than
    silently normalising a split the user did not write. The total is checked as an exact
    decimal (the literal the user wrote), not with a tolerance: 99.5% or 100.000001% is not
    100%."""
    if not isinstance(raw, Mapping) or set(raw) != set(CATEGORIES):
        return None
    shares: dict[str, float] = {}
    total = Decimal(0)
    for name in CATEGORIES:
        value = raw.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
            return None
        shares[name] = float(value)
        try:
            total += Decimal(str(value))
        except InvalidOperation:
            return None
    if total != Decimal(100):
        return None
    return shares


def autoclean_from(raw: object) -> list[str] | None:
    """Authorised categories, in canonical order; ``None`` when anything named is not a category."""
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return None
    return [name for name in CATEGORIES if name in raw]


def policy_from(raw: object) -> StoragePolicy:
    """The stored fields as a policy; every invalid piece falls back to its own default."""
    source = raw if isinstance(raw, Mapping) else {}
    policy = StoragePolicy()
    max_bytes = source.get("storage_max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not MAX_BYTES_RANGE[0] <= max_bytes <= MAX_BYTES_RANGE[1]:
        max_bytes = policy.max_bytes
    shares = shares_from(source.get("storage_shares")) or policy.shares
    alert_at = source.get("storage_alert_at")
    if not isinstance(alert_at, int) or isinstance(alert_at, bool) or not ALERT_RANGE[0] <= alert_at <= ALERT_RANGE[1]:
        alert_at = policy.alert_at
    full_policy = source.get("storage_full_policy")
    if full_policy not in FULL_POLICIES:
        full_policy = policy.full_policy
    autoclean = autoclean_from(source.get("storage_autoclean"))
    return StoragePolicy(max_bytes, dict(shares), alert_at, full_policy, tuple(autoclean) if autoclean is not None else policy.autoclean)


def check_write(policy: StoragePolicy, usage: Mapping[str, int | None], category: str, incoming: int) -> dict:
    """Whether ``incoming`` bytes fit ``category`` right now — the pre-write gate the archive layer calls.

    Fails closed: an unknown category or a usage figure the application does not actually know
    (``None``) refuses the write; a category over its own share refuses even while others sit
    empty (no borrowing); the global cap refuses on top of that. When the policy is ``autoclean``
    the ``reclaimable`` list names the authorised categories holding known bytes, so the caller
    can clean and retry — an empty list means there is nothing safe to drop.
    """
    verdict = {"allowed": False, "reason": "", "overAlert": False, "reclaimable": []}
    if category not in CATEGORIES or not isinstance(incoming, int) or isinstance(incoming, bool) or incoming < 0:
        verdict["reason"] = "unknown_category"
        return verdict
    known = all(isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool) and usage.get(name, 0) >= 0 for name in CATEGORIES)
    if not known:
        verdict["reason"] = "usage_unknown"
        verdict["reclaimable"] = reclaimable(policy, usage)
        return verdict
    total = sum(usage[name] or 0 for name in CATEGORIES)
    projected = total + incoming
    verdict["overAlert"] = projected * 100 >= policy.alert_at * policy.max_bytes
    if usage[category] + incoming > policy.budget(category):
        verdict["reason"] = "over_category"
    elif projected > policy.max_bytes:
        verdict["reason"] = "over_global"
    else:
        verdict["allowed"], verdict["reason"] = True, "ok"
    if not verdict["allowed"] and policy.full_policy == "autoclean":
        verdict["reclaimable"] = reclaimable(policy, usage)
    return verdict


def reclaimable(policy: StoragePolicy, usage: Mapping[str, int | None]) -> list[str]:
    """Authorised categories with known bytes an autoclean pass could work on — nothing when none qualify."""
    if policy.full_policy != "autoclean":
        return []
    return [name for name in policy.autoclean if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool) and (usage.get(name) or 0) > 0]


def report(policy: StoragePolicy, usage: Mapping[str, int | None]) -> dict:
    """The Storage page's payload: the split, the cap, and real occupancy — ``used`` is ``None`` for
    anything the application does not measure, so the page never draws an unknown as zero."""
    categories = []
    for name in CATEGORIES:
        used = usage.get(name)
        known = isinstance(used, int) and not isinstance(used, bool) and used >= 0
        categories.append({
            "id": name,
            "label": CATEGORY_LABELS[name],
            "share": policy.shares[name],
            "budget": policy.budget(name),
            "used": used if known else None,
            "known": known,
            "over": bool(known and used > policy.budget(name)),
        })
    total_known = all(row["known"] for row in categories)
    total = sum(row["used"] or 0 for row in categories)
    return {
        "maxBytes": policy.max_bytes,
        "alertAt": policy.alert_at,
        "fullPolicy": policy.full_policy,
        "shares": dict(policy.shares),
        "autoclean": list(policy.autoclean),
        "categories": categories,
        "total": {"used": total if total_known else None, "known": total_known, "alerted": total_known and total * 100 >= policy.alert_at * policy.max_bytes},
    }
