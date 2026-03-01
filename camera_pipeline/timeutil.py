from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_tz(dt_utc: datetime, tz_name: str) -> datetime:
    return dt_utc.astimezone(ZoneInfo(tz_name))


def is_day_enabled(local_dt: datetime, enabled_days: List[str]) -> bool:
    # Python weekday(): Monday=0
    day = DAY_NAMES[local_dt.weekday()]
    return day in enabled_days


def within_hours(local_dt: datetime, start: time, end: time) -> bool:
    """
    Supports windows that do not cross midnight (start < end) and those that do (start > end).
    """
    t = local_dt.timetz().replace(tzinfo=None)
    if start < end:
        return start <= t < end
    # Crosses midnight, e.g. 18:00 -> 02:00
    return (t >= start) or (t < end)


def next_boundary(dt_utc: datetime, interval_seconds: int) -> datetime:
    """
    Returns the next UTC datetime aligned to interval seconds.
    """
    epoch = int(dt_utc.timestamp())
    next_epoch = ((epoch // interval_seconds) + 1) * interval_seconds
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)


def capture_slot(dt_utc: datetime, interval_seconds: int) -> int:
    return int(dt_utc.timestamp()) // interval_seconds


def slot_start_utc(slot: int, interval_seconds: int) -> datetime:
    return datetime.fromtimestamp(slot * interval_seconds, tz=timezone.utc)
