from __future__ import annotations

import json
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict

from .capture import capture_still
from .storage_path import build_storage_path, storage_rel_dir
from .timeutil import (
    now_utc,
    to_tz,
    is_day_enabled,
    within_hours,
    next_boundary,
    capture_slot,
    slot_start_utc,
)

log = logging.getLogger("camera_pipeline.scheduler")


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CaptureScheduler:
    pending_root: Path
    camera_id: str
    rtsp_url: str

    interval_seconds: int
    rtsp_transport: str
    ffmpeg_stimeout_us: int
    jpeg_quality: int
    scale_width: int

    tz_name: str
    start_time: object  # datetime.time
    end_time: object    # datetime.time
    days: list[str]

    storage_bucket: str
    storage_prefix: str
    partition_timezone: str

    max_pending_files: int
    max_pending_gb: int

    delete_after_success: bool  # not used here but kept for symmetry

    def run_forever(self) -> None:
        while True:
            dt_utc = now_utc()
            dt_local = to_tz(dt_utc, self.tz_name)

            if not is_day_enabled(dt_local, self.days) or not within_hours(dt_local, self.start_time, self.end_time):
                # Sleep until next minute to re-check
                time_mod.sleep(30)
                continue

            # Align to 30s boundaries
            tick = next_boundary(dt_utc, self.interval_seconds)
            sleep_s = max(0.0, (tick - dt_utc).total_seconds())
            time_mod.sleep(sleep_s)

            # Re-check gating right before capture (handles boundary conditions)
            dt_utc = now_utc()
            dt_local = to_tz(dt_utc, self.tz_name)
            if not is_day_enabled(dt_local, self.days) or not within_hours(dt_local, self.start_time, self.end_time):
                continue

            # Compute slot for idempotency
            slot = capture_slot(dt_utc, self.interval_seconds)

            # Enforce local disk backpressure
            if not self._spool_has_capacity():
                log.warning("spool_full skipping_capture")
                continue

            self._capture_to_spool(slot)

    def _spool_has_capacity(self) -> bool:
        # Count job.json files quickly (bounded by max_pending_files)
        count = 0
        for _ in self.pending_root.rglob("job.json"):
            count += 1
            if count > self.max_pending_files:
                return False

        # Approx disk usage
        total_bytes = sum(p.stat().st_size for p in self.pending_root.rglob("*") if p.is_file())
        max_bytes = self.max_pending_gb * 1024 * 1024 * 1024
        return total_bytes <= max_bytes

    def _capture_to_spool(self, slot: int) -> None:
        storage_path = build_storage_path(
            prefix=self.storage_prefix,
            camera_id=self.camera_id,
            slot=slot,
            interval_seconds=self.interval_seconds,
            partition_timezone=self.partition_timezone,  # "local" or "utc"
            local_tz_name=self.tz_name,
        )
        rel_dir = storage_rel_dir(storage_path)
        job_dir = self.pending_root / rel_dir
        job_path = job_dir / "job.json"
        still_path = job_dir / "still.jpg"

        if job_path.exists() and still_path.exists():
            log.info("slot_already_queued camera_id=%s slot=%d", self.camera_id, slot)
            return

        slot_dt = slot_start_utc(slot, self.interval_seconds)

        try:
            result = capture_still(
                rtsp_url=self.rtsp_url,
                out_path=still_path,
                rtsp_transport=self.rtsp_transport,
                stimeout_us=self.ffmpeg_stimeout_us,
                jpeg_quality=self.jpeg_quality,
                scale_width=self.scale_width,
            )
        except Exception as e:
            log.error("capture_failed camera_id=%s slot=%d err=%s", self.camera_id, slot, str(e))
            return

        job = {
            "camera_id": self.camera_id,
            "capture_slot": slot,
            "captured_at": _to_iso_z(slot_dt),
            "storage_bucket": self.storage_bucket,
            "storage_path": storage_path,
            "bytes": result.bytes,
            "width": result.width,
            "height": result.height,
            "sha256": result.sha256,
            "attempt": 0,
            "next_attempt_at": _to_iso_z(now_utc()),
        }

        job_dir.mkdir(parents=True, exist_ok=True)
        tmp = job_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, sort_keys=True)
        tmp.replace(job_path)

        log.info("capture_ok camera_id=%s slot=%d bytes=%d storage_path=%s", self.camera_id, slot, result.bytes, storage_path)
