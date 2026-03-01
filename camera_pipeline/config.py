from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from typing import Any, Dict, List, Literal, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def _expand_env(value: str) -> str:
    """
    Expands ${VARNAME} references from the environment.
    """
    out = value
    while "${" in out:
        start = out.index("${")
        end = out.index("}", start)
        var = out[start + 2 : end]
        env_val = os.environ.get(var)
        if env_val is None:
            raise RuntimeError(f"Missing required environment variable: {var}")
        out = out[:start] + env_val + out[end + 1 :]
    return out


def _expand_env_in_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _expand_env_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_in_obj(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    rtsp_url: str


@dataclass(frozen=True)
class CaptureConfig:
    interval_seconds: int
    rtsp_transport: Literal["tcp", "udp"]
    ffmpeg_stimeout_us: int
    jpeg_quality: int
    scale_width: int


@dataclass(frozen=True)
class OperatingHoursConfig:
    timezone: str
    start: time
    end: time
    days: List[str]


@dataclass(frozen=True)
class SpoolConfig:
    root_dir: str
    max_pending_files: int
    max_pending_gb: int
    delete_after_success: bool


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    service_role_key: str
    bucket: str
    table: str
    prefix: str
    partition_timezone: Literal["local", "utc"]
    storage_upsert: bool
    cache_control_seconds: int


@dataclass(frozen=True)
class AppConfig:
    camera: CameraConfig
    capture: CaptureConfig
    operating_hours: OperatingHoursConfig
    spool: SpoolConfig
    supabase: SupabaseConfig


def load_config(path: str) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    raw = _expand_env_in_obj(raw)

    camera = CameraConfig(
        camera_id=raw["camera"]["camera_id"],
        rtsp_url=raw["camera"]["rtsp_url"],
    )

    capture = CaptureConfig(
        interval_seconds=int(raw["capture"]["interval_seconds"]),
        rtsp_transport=raw["capture"]["rtsp_transport"],
        ffmpeg_stimeout_us=int(raw["capture"]["ffmpeg_stimeout_us"]),
        jpeg_quality=int(raw["capture"]["jpeg_quality"]),
        scale_width=int(raw["capture"].get("scale_width", 0)),
    )

    op = OperatingHoursConfig(
        timezone=raw["operating_hours"]["timezone"],
        start=_parse_hhmm(raw["operating_hours"]["start"]),
        end=_parse_hhmm(raw["operating_hours"]["end"]),
        days=[d.lower() for d in raw["operating_hours"]["days"]],
    )

    spool = SpoolConfig(
        root_dir=raw["spool"]["root_dir"],
        max_pending_files=int(raw["spool"]["max_pending_files"]),
        max_pending_gb=int(raw["spool"]["max_pending_gb"]),
        delete_after_success=bool(raw["spool"]["delete_after_success"]),
    )

    sb = SupabaseConfig(
        url=raw["supabase"]["url"],
        service_role_key=raw["supabase"]["service_role_key"],
        bucket=raw["supabase"]["bucket"],
        table=raw["supabase"]["table"],
        prefix=raw["supabase"]["prefix"],
        partition_timezone=raw["supabase"]["partition_timezone"],
        storage_upsert=bool(raw["supabase"]["storage_upsert"]),
        cache_control_seconds=int(raw["supabase"]["cache_control_seconds"]),
    )

    return AppConfig(
        camera=camera,
        capture=capture,
        operating_hours=op,
        spool=spool,
        supabase=sb,
    )
