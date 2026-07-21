"""UTC persistence helpers for the daily Asia/Shanghai price schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_daily_refresh(now: datetime, *, local_hour: int = 3) -> datetime:
    """Return the next Beijing-time daily slot as a naive UTC datetime."""

    aware_utc = now.replace(tzinfo=UTC)
    local_now = aware_utc.astimezone(_SHANGHAI)
    target = local_now.replace(hour=local_hour, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(UTC).replace(tzinfo=None)


def current_beijing_day_start(now: datetime) -> datetime:
    """Return the current Beijing calendar day's start as naive UTC."""

    local_now = now.replace(tzinfo=UTC).astimezone(_SHANGHAI)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)
