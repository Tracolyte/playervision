from __future__ import annotations

import json
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from .spool import pending_job_paths, write_health, count_pending_jobs, bytes_in_tree
from .supabase_io import SupabaseHandle, upload_jpeg, upsert_still_row


log = logging.getLogger("camera_pipeline.uploader")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _backoff_seconds(attempt: int, base: int = 5, cap: int = 300) -> int:
    # Exponential backoff with cap: 5,10,20,40,... up to 300s
    return min(cap, base * (2 ** max(0, attempt - 1)))


@dataclass
class UploadWorker:
    pending_root: Path
    health_path: Path
    sb: SupabaseHandle
    on_conflict: str
    delete_after_success: bool

    cache_control_seconds: int
    storage_upsert: bool

    def run_forever(self) -> None:
        while True:
            did_work = self._drain_once()
            if not did_work:
                # No eligible jobs; sleep lightly.
                time_mod.sleep(2)

    def _drain_once(self) -> bool:
        jobs = sorted(pending_job_paths(self.pending_root))
        now = _utc_now()

        for job_path in jobs:
            try:
                with job_path.open("r", encoding="utf-8") as f:
                    job = json.load(f)
            except Exception as e:
                log.error("failed_to_read_job job=%s err=%s", str(job_path), str(e))
                continue

            next_attempt = _parse_dt(job.get("next_attempt_at", "1970-01-01T00:00:00+00:00"))
            if next_attempt > now:
                continue

            ok = self._process_job(job_path, job)
            self._write_health()
            return True

        self._write_health()
        return False

    def _process_job(self, job_path: Path, job: Dict) -> bool:
        still_path = job_path.parent / "still.jpg"
        if not still_path.exists():
            log.error("missing_still job=%s still=%s", str(job_path), str(still_path))
            return False

        attempt = int(job.get("attempt", 0)) + 1
        job["attempt"] = attempt
        job["last_attempt_at"] = _to_iso_z(_utc_now())

        storage_path = job["storage_path"]

        try:
            uploaded_path = upload_jpeg(
                sb=self.sb,
                storage_path=storage_path,
                local_file_path=str(still_path),
                cache_control_seconds=self.cache_control_seconds,
                upsert=self.storage_upsert,
            )
            job["uploaded_path"] = uploaded_path
            job["upload_status"] = "ok"
        except Exception as e:
            delay = _backoff_seconds(attempt)
            job["upload_status"] = "error"
            job["error"] = f"upload_failed: {e}"
            job["next_attempt_at"] = _to_iso_z(_utc_now() + timedelta(seconds=delay))
            self._atomic_job_write(job_path, job)
            log.warning("upload_failed storage_path=%s attempt=%d err=%s next_in=%ss", storage_path, attempt, str(e), delay)
            return False

        # Upsert DB row *after* successful upload
        row = {
            "camera_id": job["camera_id"],
            "capture_slot": job["capture_slot"],
            "captured_at": job["captured_at"],
            "storage_bucket": job["storage_bucket"],
            "storage_path": storage_path,
            "bytes": job["bytes"],
            "width": job.get("width"),
            "height": job.get("height"),
            "sha256": job.get("sha256"),
            "status": "indexed",
            "error": None,
        }

        try:
            upsert_still_row(
                sb=self.sb,
                row=row,
                on_conflict=self.on_conflict,
            )
            job["db_status"] = "ok"
        except Exception as e:
            delay = _backoff_seconds(attempt)
            job["db_status"] = "error"
            job["error"] = f"db_upsert_failed: {e}"
            job["next_attempt_at"] = _to_iso_z(_utc_now() + timedelta(seconds=delay))
            self._atomic_job_write(job_path, job)
            log.warning("db_upsert_failed storage_path=%s attempt=%d err=%s next_in=%ss", storage_path, attempt, str(e), delay)
            return False

        # Success: cleanup
        if self.delete_after_success:
            try:
                still_path.unlink(missing_ok=True)
                job_path.unlink(missing_ok=True)
                # Cleanup empty directories upward, but do not delete pending_root itself
                self._cleanup_empty_parents(job_path.parent)
            except Exception as e:
                log.error("cleanup_failed dir=%s err=%s", str(job_path.parent), str(e))

        log.info("job_success camera_id=%s slot=%s storage_path=%s", job["camera_id"], job["capture_slot"], storage_path)
        return True

    def _cleanup_empty_parents(self, start_dir: Path) -> None:
        cur = start_dir
        while cur != self.pending_root:
            try:
                cur.rmdir()
            except OSError:
                return
            cur = cur.parent

    def _atomic_job_write(self, job_path: Path, job: Dict) -> None:
        tmp = job_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, sort_keys=True)
        tmp.replace(job_path)

    def _write_health(self) -> None:
        payload = {
            "ts": _to_iso_z(_utc_now()),
            "pending_jobs": count_pending_jobs(self.pending_root),
            "pending_bytes": bytes_in_tree(self.pending_root),
        }
        try:
            write_health(self.health_path, payload)
        except Exception as e:
            log.error("health_write_failed err=%s", str(e))
