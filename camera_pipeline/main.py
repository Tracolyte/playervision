from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from .config import load_config
from .logging_setup import setup_logging
from .spool import init_spool
from .supabase_io import init_supabase
from .scheduler import CaptureScheduler
from .uploader import UploadWorker

log = logging.getLogger("camera_pipeline.main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Path to TOML config")
    p.add_argument("--once", action="store_true", help="Run one capture and one drain cycle then exit")
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    cfg = load_config(args.config)
    spool = init_spool(cfg.spool.root_dir)

    sb = init_supabase(
        url=cfg.supabase.url,
        service_role_key=cfg.supabase.service_role_key,
        bucket=cfg.supabase.bucket,
        table=cfg.supabase.table,
    )

    scheduler = CaptureScheduler(
        pending_root=spool.pending,
        camera_id=cfg.camera.camera_id,
        rtsp_url=cfg.camera.rtsp_url,
        interval_seconds=cfg.capture.interval_seconds,
        rtsp_transport=cfg.capture.rtsp_transport,
        ffmpeg_stimeout_us=cfg.capture.ffmpeg_stimeout_us,
        jpeg_quality=cfg.capture.jpeg_quality,
        scale_width=cfg.capture.scale_width,
        tz_name=cfg.operating_hours.timezone,
        start_time=cfg.operating_hours.start,
        end_time=cfg.operating_hours.end,
        days=cfg.operating_hours.days,
        storage_bucket=cfg.supabase.bucket,
        storage_prefix=cfg.supabase.prefix,
        partition_timezone=cfg.supabase.partition_timezone,
        max_pending_files=cfg.spool.max_pending_files,
        max_pending_gb=cfg.spool.max_pending_gb,
        delete_after_success=cfg.spool.delete_after_success,
    )

    uploader = UploadWorker(
        pending_root=spool.pending,
        health_path=spool.health,
        sb=sb,
        on_conflict="camera_id,capture_slot",
        delete_after_success=cfg.spool.delete_after_success,
        cache_control_seconds=cfg.supabase.cache_control_seconds,
        storage_upsert=cfg.supabase.storage_upsert,
    )

    if args.once:
        # Capture once: run scheduler capture path by calling _capture_to_spool for current slot.
        # We'll just start uploader drain after a short capture loop.
        log.info("running_once")
        t1 = threading.Thread(target=lambda: scheduler.run_forever(), daemon=True)
        t2 = threading.Thread(target=lambda: uploader.run_forever(), daemon=True)
        t1.start()
        t2.start()
        # Run for 35s then exit
        import time
        time.sleep(35)
        return 0

    # Run forever
    t_cap = threading.Thread(target=scheduler.run_forever, daemon=True)
    t_up = threading.Thread(target=uploader.run_forever, daemon=True)

    t_cap.start()
    t_up.start()

    t_cap.join()
    t_up.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
