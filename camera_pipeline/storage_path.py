from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Literal

from .timeutil import slot_start_utc


def build_storage_path(
    prefix: str,
    camera_id: str,
    slot: int,
    interval_seconds: int,
    partition_timezone: Literal["local", "utc"],
    local_tz_name: str,
) -> str:
    """
    Returns:
      stills/<camera_id>/<YYYY>/<MM>/<DD>/<camera_id>_<slot_ts>.jpg
    """
    slot_dt_utc = slot_start_utc(slot, interval_seconds)

    if partition_timezone == "local":
        dt = slot_dt_utc.astimezone(ZoneInfo(local_tz_name))
    else:
        dt = slot_dt_utc

    y = dt.strftime("%Y")
    m = dt.strftime("%m")
    d = dt.strftime("%d")

    filename = f"{camera_id}_{slot_dt_utc.strftime('%Y%m%dT%H%M%SZ')}.jpg"
    return f"{prefix}/{camera_id}/{y}/{m}/{d}/{filename}"


def storage_rel_dir(storage_path: str) -> str:
    # directory portion: stills/cam/2026/03/01
    parts = storage_path.split("/")
    return "/".join(parts[:-1])
