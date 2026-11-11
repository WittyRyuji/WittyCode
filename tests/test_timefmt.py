from __future__ import annotations

from datetime import datetime, timedelta, timezone

from desktop.core.timefmt import format_relative

NOW = datetime(2026, 3, 15, 12, 0, 0).astimezone()  # aware, local zone


def ago(**kwargs):
    return format_relative(NOW - timedelta(**kwargs), NOW)


def test_recent_times():
    assert ago(seconds=5) == "刚刚"
    assert ago(minutes=5) == "5 分钟前"
    assert ago(minutes=59) == "59 分钟前"
    assert ago(hours=3) == "3 小时前"


def test_future_times_are_clamped_to_now():
    assert format_relative(NOW + timedelta(minutes=10), NOW) == "刚刚"


def test_yesterday_and_older_use_local_calendar_days():
    yesterday = NOW.replace(hour=9, minute=5) - timedelta(days=1)
    assert format_relative(yesterday, NOW) == "昨天 09:05"
    earlier = NOW.replace(hour=9, minute=5) - timedelta(days=20)
    assert format_relative(earlier, NOW) == earlier.strftime("%m-%d 09:05")
    last_year = NOW.replace(year=2025, month=12, day=31)
    assert format_relative(last_year, NOW) == "2025-12-31"


def test_naive_datetimes_are_treated_as_utc():
    now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
    assert format_relative(datetime(2026, 3, 15, 11, 30), now) == "30 分钟前"