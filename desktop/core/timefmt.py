"""Relative time strings for the session list."""

from __future__ import annotations

from datetime import datetime, timezone


def format_relative(then: datetime, now: datetime | None = None) -> str:
    """``刚刚`` / ``5 分钟前`` / ``3 小时前`` / ``昨天 14:20`` / ``10-28 09:05`` / ``2025-12-31``.

    Naive datetimes are taken as UTC. Day boundaries use the machine's local time zone.
    """
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = (now - then).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    local_then, local_now = then.astimezone(), now.astimezone()
    days = (local_now.date() - local_then.date()).days
    if days == 0:
        return f"{int(seconds // 3600)} 小时前"
    if days == 1:
        return f"昨天 {local_then:%H:%M}"
    if local_then.year == local_now.year:
        return f"{local_then:%m-%d %H:%M}"
    return f"{local_then:%Y-%m-%d}"
