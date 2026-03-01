from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple


@dataclass(frozen=True)
class SpoolPaths:
    root: Path
    pending: Path
    health: Path


def init_spool(root_dir: str) -> SpoolPaths:
    root = Path(root_dir)
    pending = root / "pending"
    health = root / "health.json"

    pending.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    return SpoolPaths(root=root, pending=pending, health=health)


def _atomic_write_json(path: Path, payload: Dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def write_health(health_path: Path, payload: Dict) -> None:
    _atomic_write_json(health_path, payload)


def pending_job_paths(pending_root: Path) -> Iterator[Path]:
    # Find all job.json files under pending/
    yield from pending_root.rglob("job.json")


def job_dir_for(pending_root: Path, storage_rel_dir: str) -> Path:
    # storage_rel_dir like: stills/cam/2026/03/01
    return pending_root / storage_rel_dir


def bytes_in_tree(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def count_pending_jobs(pending_root: Path) -> int:
    return sum(1 for _ in pending_job_paths(pending_root))


def remove_tree_if_empty(path: Path, stop_at: Path) -> None:
    """
    Remove path and its parents if empty, but do not go above stop_at.
    """
    cur = path
    while True:
        if cur == stop_at:
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def delete_job_artifacts(job_path: Path) -> None:
    """
    job.json is stored in the same directory as still.jpg
    """
    job_dir = job_path.parent
    still_path = job_dir / "still.jpg"

    if still_path.exists():
        still_path.unlink(missing_ok=True)
    job_path.unlink(missing_ok=True)

    # Cleanup empty directories up to pending/
    remove_tree_if_empty(job_dir, stop_at=job_dir.parents[len(job_dir.parents) - 1])
