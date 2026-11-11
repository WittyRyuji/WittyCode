"""The Codex subscription as the page shows it (``frontend/src/types.ts`` ``CodexAccount`` / ``LimitWindow``).

Pure: built from what ``account/read`` and ``account/rateLimits/read`` answer. Shapes (codex-cli 0.165.0):

- ``account/read`` -> ``{"account": null | {"type": "chatgpt", "email", "planType"} | {"type": "apiKey"} |
  {"type": "amazonBedrock", ...} | {"type": "gcp"}, "requiresOpenaiAuth": bool, ...}`` (signed out: ``account`` null,
  verified).
- ``account/rateLimits/read`` -> ``{"rateLimits": snapshot, "rateLimitsByLimitId": {"codex": snapshot, ...}, ...}`` with
  ``snapshot = {"limitId", "primary": window | null, "secondary": window | null, "planType", ...}`` and
  ``window = {"usedPercent", "windowDurationMins", "resetsAt" (Unix seconds)}``; signed out it is an error (verified:
  ``-32600 codex account authentication required to read rate limits``). The window shapes come from the generated
  schema only: no signed-in account was available.

``enabled`` is not set here: the backend adds it from the settings.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

WINDOW_LABELS: dict[int, tuple[str, str]] = {
    300: ("5 小时", "5h"),
    1440: ("每天", "1d"),
    10080: ("每周", "1w"),
    43200: ("每月", "1mo"),
}
PLAN_LABELS: dict[str, str] = {
    "free": "ChatGPT Free",
    "go": "ChatGPT Go",
    "plus": "ChatGPT Plus",
    "pro": "ChatGPT Pro",
    "prolite": "ChatGPT Pro Lite",
    "promax": "ChatGPT Pro Max",
    "team": "ChatGPT Team",
    "business": "ChatGPT Business",
    "enterprise": "ChatGPT Enterprise",
    "edu": "ChatGPT Edu",
}
_PLAN_PREFIXES = (("self_serve_business", "ChatGPT Business"), ("enterprise", "ChatGPT Enterprise"), ("ent", "ChatGPT Enterprise"), ("edu", "ChatGPT Edu"))
MAIN_LIMIT = "codex"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def window_label(minutes: Any) -> tuple[str, str]:
    """``(label, short)`` of a quota window: ``("5 小时", "5h")``, ``("每周", "1w")``, ``("3 天", "3d")`` ..."""
    value = _number(minutes)
    if value is None or value <= 0:
        return ("额度", "limit")
    total = int(round(value))
    if total in WINDOW_LABELS:
        return WINDOW_LABELS[total]
    if total % 10080 == 0:
        return (f"{total // 10080} 周", f"{total // 10080}w")
    if total % 1440 == 0:
        return (f"{total // 1440} 天", f"{total // 1440}d")
    if total % 60 == 0:
        return (f"{total // 60} 小时", f"{total // 60}h")
    return (f"{total} 分钟", f"{total}m")


def resets_in_text(resets_at: Any, now: float) -> str:
    """``3 小时 12 分后重置`` / ``4 天 6 小时后重置`` / ``12 分钟后重置`` / ``即将重置`` (empty when unknown)."""
    moment = _number(resets_at)
    if moment is None:
        return ""
    seconds = moment - now
    if seconds < 60:
        return "即将重置"
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时后重置" if hours else f"{days} 天后重置"
    if hours:
        return f"{hours} 小时 {mins} 分后重置" if mins else f"{hours} 小时后重置"
    return f"{mins} 分钟后重置"


def _percent(value: Any) -> int:
    number = _number(value)
    if number is None:
        return 0
    return max(0, min(100, int(round(number))))


def main_snapshot(rate_limits: Any) -> Mapping[str, Any] | None:
    """The ``codex`` bucket of an ``account/rateLimits/read`` result (or a bare snapshot, as notifications carry)."""
    if not isinstance(rate_limits, Mapping):
        return None
    if "rateLimits" in rate_limits or "rateLimitsByLimitId" in rate_limits:
        by_id = rate_limits.get("rateLimitsByLimitId")
        if isinstance(by_id, Mapping) and isinstance(by_id.get(MAIN_LIMIT), Mapping):
            return by_id[MAIN_LIMIT]
        snapshot = rate_limits.get("rateLimits")
        return snapshot if isinstance(snapshot, Mapping) else None
    return rate_limits


def limit_windows(rate_limits: Any, *, now: float) -> list[dict[str, Any]]:
    """The quota windows, shortest first (the 5-hour one, then the weekly one)."""
    snapshot = main_snapshot(rate_limits)
    if snapshot is None:
        return []
    windows = [window for window in (snapshot.get("primary"), snapshot.get("secondary")) if isinstance(window, Mapping)]
    windows.sort(key=lambda window: _number(window.get("windowDurationMins")) or float("inf"))
    out: list[dict[str, Any]] = []
    for window in windows:
        label, short = window_label(window.get("windowDurationMins"))
        out.append(
            {
                "label": label,
                "short": short,
                "usedPercent": _percent(window.get("usedPercent")),
                "resetsIn": resets_in_text(window.get("resetsAt"), now),
            }
        )
    return out


def merge_rate_limits(previous: Any, update: Any) -> dict[str, Any]:
    """Fold an ``account/rateLimits/updated`` snapshot (sparse: missing values are unknown, not cleared) into the
    last ``account/rateLimits/read`` result."""
    base: dict[str, Any] = dict(previous) if isinstance(previous, Mapping) else {}
    if not isinstance(update, Mapping):
        return base
    limit_id = update.get("limitId") or MAIN_LIMIT
    by_id: dict[str, Any] = dict(base.get("rateLimitsByLimitId") or {})
    current = by_id.get(limit_id)
    if not isinstance(current, Mapping) and limit_id == MAIN_LIMIT:
        current = base.get("rateLimits")
    merged = dict(current) if isinstance(current, Mapping) else {}
    for key, value in update.items():
        if value is not None:
            merged[key] = value
    by_id[limit_id] = merged
    base["rateLimitsByLimitId"] = by_id
    if limit_id == MAIN_LIMIT:
        base["rateLimits"] = merged
    return base


def plan_label(plan: Any) -> str | None:
    if not isinstance(plan, str) or not plan or plan == "unknown":
        return None
    if plan in PLAN_LABELS:
        return PLAN_LABELS[plan]
    for prefix, label in _PLAN_PREFIXES:
        if plan.startswith(prefix):
            return label
    return "ChatGPT " + plan.replace("_", " ").title()


def auth_state(account: Any, *, signing_in: bool = False) -> str:
    """``signing-in`` while a browser sign-in is under way; ``chatgpt`` for a subscription; ``api-key`` for any other
    credentials (an API key, Bedrock, Vertex: no subscription limits); ``signed-out`` otherwise."""
    if signing_in:
        return "signing-in"
    kind = account.get("type") if isinstance(account, Mapping) else None
    if kind == "chatgpt":
        return "chatgpt"
    if kind:
        return "api-key"
    return "signed-out"


def account_json(
    binary: Any,
    account: Any = None,
    rate_limits: Any = None,
    *,
    signing_in: bool = False,
    now: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """``binary``: a ``CodexBinary`` (``None``: no CLI). ``account``: the ``account/read`` result."""
    if binary is None:
        return {"cli": "missing", "auth": "signed-out", "limits": []}
    moment = time.time() if now is None else float(now)
    data: dict[str, Any] = {"cli": "found", "cliPath": str(binary.path), "version": binary.version, "limits": []}
    details = account.get("account") if isinstance(account, Mapping) else None
    data["auth"] = auth_state(details, signing_in=signing_in)
    if isinstance(details, Mapping) and details.get("type") == "chatgpt":
        if details.get("email"):
            data["email"] = str(details["email"])
        plan = plan_label(details.get("planType"))
        if plan:
            data["plan"] = plan
        data["limits"] = limit_windows(rate_limits, now=moment)
    if error:
        data["error"] = error
    return data


def is_chatgpt(account: Any) -> bool:
    details = account.get("account") if isinstance(account, Mapping) else None
    return isinstance(details, Mapping) and details.get("type") == "chatgpt"


def is_signed_out(account: Any) -> bool:
    """No credentials although the provider needs OpenAI's (``requiresOpenaiAuth``)."""
    if not isinstance(account, Mapping):
        return False
    return account.get("account") is None and bool(account.get("requiresOpenaiAuth", True))
