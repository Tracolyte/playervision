# Implementation Plan for a Raspberry Pi RTSP Still-Capture Pipeline Using Supabase

## Product requirements and success metrics

### Objective and background
The MVP goal is a minimal, reliable ingestion pipeline that captures one still image every 30 seconds from an on-site IP camera RTSP stream, stores the image in object storage, and writes a metadata record into a Postgres-backed database for later analysis and product features. The camera RTSP stream format and the need to enable the RTSP port/UI settings are documented by the camera vendor (including distinct “main stream” and “sub stream” RTSP URLs). citeturn4view5turn4view6turn0search4

This plan assumes:
- One camera on a local LAN (gym Wi‑Fi/Ethernet).
- One always-on Raspberry Pi running Raspberry Pi OS and managed via systemd.
- Supabase providing Storage + Postgres access through the Python client library. citeturn9view0turn13view0

### Functional requirements (MVP)
- **Capture**: pull a single frame over RTSP at a 30-second cadence, preferably from the camera “sub stream” for stability/bandwidth. Reolink provides RTSP URL patterns for main/sub streams (the exact path may vary by firmware/model). citeturn4view5turn0search4  
- **Scheduling**: capture runs only during configurable “operating hours” in a configured timezone (default: America/New_York).
- **Storage**: upload each JPEG to a deterministic path in a Supabase Storage bucket. Supabase Storage supports overwriting behavior via an upsert option when needed (but warns against overwriting when possible due to CDN propagation / stale content concerns). citeturn11view2
- **Database write**: write one row per still with metadata and the storage path using an idempotent scheme. Supabase Python upsert supports specifying `on_conflict` columns that must correspond to a UNIQUE constraint. citeturn4view2turn12search27  
- **Reliability**: if upload or DB write fails, retain the still locally and retry with backoff.  
- **Observability**: structured logs + a “health” file (last success timestamps, pending queue size) and systemd-based restart behavior. systemd supports restart policies such as `Restart=on-failure`. citeturn6search28turn6search1

### Non-goals (MVP)
- No real-time streaming to clients.
- No continuous recording in MVP.
- No on-device inference.
- No multi-camera orchestration.

### Success criteria
- Runs unattended for 7 days.
- Captures every 30 seconds during operating hours with >99% success over that window.
- Survives reboots and transient network outages without manual intervention.
- “DB reflects storage” with near-zero orphaned records/files; the design below biases toward only inserting DB rows after storage upload succeeds, then retrying DB insert until it succeeds. (This makes orphaned *storage objects* possible temporarily but strongly reduces orphaned *DB rows*.)

## System architecture and data contracts

### High-level design
The architecture is a single long-running service on the Pi:

1. **Capture loop (time-gated)**  
   RTSP → FFmpeg frame grab → local spool (JPEG + JSON job metadata)

2. **Upload/index loop (always-on)**  
   local spool queue → Supabase Storage upload → Supabase Postgres upsert → cleanup local spool

This decoupling ensures the Pi continues capturing on schedule even if internet/upload is temporarily down, as long as local disk has space.

image_group{"layout":"carousel","aspect_ratio":"16:9","query":["Raspberry Pi 5 board photo","Reolink E1 Pro indoor IP camera","Supabase Storage logo","RTSP camera to cloud pipeline diagram"],"num_per_query":1}

### Interfaces and contracts

#### RTSP input contract
Reolink documents RTSP URLs for main/sub streams (commonly `/Preview_01_main` and `/Preview_01_sub`), and also references an alternate URL format using `/h264Preview_01_main` in vendor materials. In practice, you should test which path works for your camera firmware, and store the working RTSP URL in a secret env file. citeturn4view5turn0search4

Reolink also notes: avoid special characters in the password for RTSP reliability. citeturn4view5

#### Storage output contract (Supabase Storage)
- One bucket (e.g., `camera-stills`)
- Object key convention:

`stills/<camera_id>/<YYYY>/<MM>/<DD>/<camera_id>_<slot_ts>.jpg`

Where `slot_ts` is the UTC start timestamp of the 30-second capture slot, formatted like `20260301T134500Z`.

Supabase Storage bucket access models:
- Buckets are private by default; in private buckets, operations and downloads are subject to RLS policies. citeturn11view1
- Private bucket assets require signed URLs or authenticated downloads; public buckets can be accessed with a conventional public URL format. citeturn11view0turn11view1
- If you run Storage operations from a trusted server (the Pi), you can use a service key which bypasses Storage access controls, and must not be shared publicly. citeturn4view3

#### Database output contract (Postgres via Supabase Data API)
One table: `camera_stills` with an idempotency key.

Idempotency strategy:
- Compute `capture_slot := floor(epoch_seconds_utc / 30)` on the Pi.
- Enforce `UNIQUE(camera_id, capture_slot)` in Postgres.
- Upsert on these columns from the Pi. Supabase Python upsert supports an `on_conflict` parameter for this purpose. citeturn4view2turn12search27

Postgres implements uniqueness constraints using unique indexes; defining a unique constraint automatically creates the supporting unique index. citeturn12search1turn12search29

### Sequence diagrams

#### Capture and enqueue
```mermaid
sequenceDiagram
  participant S as Scheduler
  participant C as Capture (ffmpeg)
  participant FS as Local Spool FS
  S->>S: If inside operating hours and on 30s tick
  S->>C: Run ffmpeg RTSP frame grab
  C->>FS: Write still.jpg
  S->>FS: Write job.json (metadata, slot, retry state)
```

#### Upload and index with retries
```mermaid
sequenceDiagram
  participant W as Upload Worker
  participant FS as Local Spool FS
  participant ST as Supabase Storage
  participant DB as Supabase Postgres
  W->>FS: Pick next eligible job.json
  W->>ST: Upload still.jpg to bucket/path
  ST-->>W: Success (path) OR error
  alt Upload success
    W->>DB: Upsert camera_stills on (camera_id,capture_slot)
    DB-->>W: Success OR error
    alt DB success
      W->>FS: Delete job.json and still.jpg (or archive)
    else DB error
      W->>FS: Keep files; update next_attempt_at with backoff
    end
  else Upload error
    W->>FS: Keep files; update next_attempt_at with backoff
  end
```

## Supabase setup with schema, indexes, and security

### Storage bucket
Create a bucket named `camera-stills`.

Recommended for MVP:
- **Private bucket** (default) for least accidental exposure. Buckets are private by default, and private bucket downloads require signed URLs or authenticated access. citeturn11view1turn11view0  
- Use the Supabase **service role key** on the Pi (trusted environment) to upload and bypass Storage RLS policies. Supabase explicitly documents service key bypass for Storage, and warns not to share the service key publicly. citeturn4view3

If you later need to display images in an admin UI:
- Generate signed URLs server-side using the SDK (`create_signed_url` exists in the Python docs). citeturn4view1turn11view0

### Service role key handling
Supabase notes that service keys can bypass RLS and should not be exposed to browsers/customers. citeturn2search0turn4view3  
Supabase also documents that RLS enforcement depends on the `Authorization` header and explains common mistakes where a user session overrides a service-role client. For this pipeline, avoid auth session flows entirely; use a dedicated client initialized only with the service role key. citeturn4view4

### Database schema
Run the following SQL in the Supabase SQL editor:

```sql
-- Enable gen_random_uuid() for UUID primary keys
create extension if not exists pgcrypto;

create table if not exists public.camera_stills (
  id uuid primary key default gen_random_uuid(),

  camera_id text not null,

  -- Idempotency key: 30-second slot number in UTC epoch seconds
  capture_slot bigint not null,

  -- Timestamp for the slot start or the capture time (store UTC)
  captured_at timestamptz not null,

  storage_bucket text not null,
  storage_path text not null,

  bytes integer not null,
  width integer,
  height integer,
  sha256 text,

  status text not null default 'indexed',
  error text,

  created_at timestamptz not null default now(),

  constraint camera_stills_unique_slot unique (camera_id, capture_slot)
);

create index if not exists camera_stills_captured_at_idx
  on public.camera_stills (captured_at desc);

create index if not exists camera_stills_camera_id_captured_at_idx
  on public.camera_stills (camera_id, captured_at desc);
```

Why this works:
- The UNIQUE constraint enforces idempotency for `(camera_id, capture_slot)`; Postgres enforces uniqueness using unique indexes. citeturn12search1turn12search29
- Upsert behavior relies on Postgres `INSERT ... ON CONFLICT` semantics; Postgres documents `ON CONFLICT DO UPDATE` behavior and its determinism properties. citeturn12search0turn12search12
- Supabase Python upsert supports specifying `on_conflict` columns, which must correspond to a UNIQUE constraint. citeturn4view2turn12search27

## Raspberry Pi implementation with complete code

### Implementation choices and rationale
- **Language**: Python 3 (supported by the Supabase Python client; requires Python ≥3.9). citeturn13view0turn9view0
- **Capture**: use FFmpeg CLI to grab a single JPEG frame from RTSP. FFmpeg documentation shows using `-vframes 1` (or `-frames:v 1`) with the `image2` muxer to generate a single still image. citeturn17search3turn17search10
- **RTSP transport**: default to TCP (`-rtsp_transport tcp`) for stability on lossy Wi‑Fi; FFmpeg includes explicit TCP RTSP examples, and FFmpeg RTSP options include transport selection. citeturn1search20turn17search15
- **Timeouts**: use RTSP socket I/O timeout options (commonly `-stimeout` in microseconds); FFmpeg user documentation describes `-stimeout` for RTSP as a TCP I/O timeout in microseconds. citeturn17search5turn17search12
- **Supabase SDK**: use the open-source Supabase Python client (`supabase-py`). citeturn10view0turn9view0  
  Pin the version for reproducibility; as of Feb 10, 2026, `supabase 2.28.0` is published on PyPI. citeturn13view0  
- **Reliability**: local spool directory is the “source of truth” for retrying uploads/inserts until success.

### Repository layout (create exactly these files)

#### `requirements.txt`
```txt
supabase==2.28.0
Pillow==10.4.0
tomli==2.0.1; python_version < "3.11"
```

#### `README.md`
```md
# camera-pipeline (MVP)

Captures one JPEG still from an RTSP camera every N seconds during operating hours,
uploads to Supabase Storage, and upserts metadata to Supabase Postgres.

## Quick start (after installing on the Pi)

1) Copy config and env
- sudo mkdir -p /etc/camera-pipeline
- sudo cp config.example.toml /etc/camera-pipeline/config.toml
- sudo cp camera-pipeline.env.example /etc/camera-pipeline/camera-pipeline.env
- sudo chmod 600 /etc/camera-pipeline/camera-pipeline.env

2) Edit /etc/camera-pipeline/camera-pipeline.env with:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- CAMERA_RTSP_URL

3) Test one capture
/opt/camera-pipeline/venv/bin/python -m camera_pipeline.main --config /etc/camera-pipeline/config.toml --once

4) Run as a service
sudo cp deploy/camera-pipeline.service /etc/systemd/system/camera-pipeline.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-pipeline

5) View logs
journalctl -u camera-pipeline -f
```

#### `config.example.toml`
```toml
[camera]
camera_id = "gym_cam_1"
# Keep credentials out of this file; set CAMERA_RTSP_URL in the env file.
rtsp_url = "${CAMERA_RTSP_URL}"

[capture]
interval_seconds = 30
rtsp_transport = "tcp"            # "tcp" or "udp"
ffmpeg_stimeout_us = 7000000      # 7 seconds
jpeg_quality = 2                  # 2 is high quality; larger is more compression
scale_width = 0                   # 0 disables scaling; set to e.g. 1280 to downscale

[operating_hours]
timezone = "America/New_York"
start = "06:00"
end = "22:00"
days = ["mon","tue","wed","thu","fri","sat","sun"]

[spool]
root_dir = "/var/lib/camera-pipeline"
max_pending_files = 20000
max_pending_gb = 10
delete_after_success = true

[supabase]
# Keep secrets out of this file; set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the env file.
url = "${SUPABASE_URL}"
service_role_key = "${SUPABASE_SERVICE_ROLE_KEY}"

bucket = "camera-stills"
table = "camera_stills"

# Storage path convention
prefix = "stills"
partition_timezone = "local"     # "local" or "utc"

# If a duplicate capture slot is retried, allow overwriting the same object key.
# Supabase supports upsert on upload, but advises against overwriting when possible due to CDN propagation delays.
# For this pipeline, overwrites should be very rare (mainly restarts), and object keys are normally unique per slot.
storage_upsert = true
cache_control_seconds = 3600
```

#### `camera-pipeline.env.example`
```bash
# Supabase
SUPABASE_URL="https://YOUR_PROJECT_REF.supabase.co"
SUPABASE_SERVICE_ROLE_KEY="YOUR_SERVICE_ROLE_KEY"

# Camera RTSP URL: choose ONE that works for your E1 Pro firmware.
# Reolink documented examples include:
#   rtsp://user:pass@ip_address:554/Preview_01_sub
#   rtsp://user:pass@ip_address:554/h264Preview_01_sub
CAMERA_RTSP_URL="rtsp://admin:YOUR_PASSWORD@192.168.1.50:554/Preview_01_sub"
```

#### `deploy/camera-pipeline.service`
```ini
[Unit]
Description=Camera still capture pipeline (RTSP -> Supabase Storage -> Postgres)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=camera-pipeline
Group=camera-pipeline

EnvironmentFile=/etc/camera-pipeline/camera-pipeline.env
ExecStart=/opt/camera-pipeline/venv/bin/python -m camera_pipeline.main --config /etc/camera-pipeline/config.toml

WorkingDirectory=/opt/camera-pipeline
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/camera-pipeline
ReadOnlyPaths=/etc/camera-pipeline

[Install]
WantedBy=multi-user.target
```

#### `camera_pipeline/__init__.py`
```py
__all__ = []
```

#### `camera_pipeline/config.py`
```py
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
```

#### `camera_pipeline/timeutil.py`
```py
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
```

#### `camera_pipeline/spool.py`
```py
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
```

> Note: `delete_job_artifacts` cleans the immediate job directory; we’ll call an additional cleanup to remove empty parents safely in the uploader.

#### `camera_pipeline/storage_path.py`
```py
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
```

#### `camera_pipeline/capture.py`
```py
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image


@dataclass(frozen=True)
class CaptureResult:
    bytes: int
    width: int
    height: int
    sha256: str


def _run_ffmpeg_grab(
    rtsp_url: str,
    out_path: Path,
    rtsp_transport: str,
    stimeout_us: int,
    jpeg_quality: int,
    scale_width: int,
) -> None:
    """
    Uses ffmpeg to capture a single frame from RTSP and write it as JPEG.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = []
    if scale_width and scale_width > 0:
        # Keep aspect ratio; scale to width, auto height.
        vf.append(f"scale={scale_width}:-2")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", rtsp_transport,
        "-stimeout", str(stimeout_us),
        "-i", rtsp_url,
        "-an",
        "-frames:v", "1",
        "-q:v", str(jpeg_quality),
    ]
    if vf:
        cmd += ["-vf", ",".join(vf)]

    # Force image2 muxer and output path; overwrite local file if any.
    cmd += ["-f", "image2", "-y", str(out_path)]

    subprocess.run(cmd, check=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_still(
    rtsp_url: str,
    out_path: Path,
    rtsp_transport: str,
    stimeout_us: int,
    jpeg_quality: int,
    scale_width: int,
) -> CaptureResult:
    _run_ffmpeg_grab(
        rtsp_url=rtsp_url,
        out_path=out_path,
        rtsp_transport=rtsp_transport,
        stimeout_us=stimeout_us,
        jpeg_quality=jpeg_quality,
        scale_width=scale_width,
    )

    b = out_path.stat().st_size
    with Image.open(out_path) as im:
        w, h = im.size

    digest = _sha256_file(out_path)
    return CaptureResult(bytes=b, width=w, height=h, sha256=digest)
```

#### `camera_pipeline/supabase_io.py`
```py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from supabase import create_client, Client
from supabase.client import ClientOptions


@dataclass(frozen=True)
class SupabaseHandle:
    client: Client
    bucket: str
    table: str


def init_supabase(url: str, service_role_key: str, bucket: str, table: str) -> SupabaseHandle:
    # Use explicit timeouts so transient slowness doesn't block forever.
    # Supabase Python docs show ClientOptions supports postgrest and storage timeouts.
    opts = ClientOptions(
        postgrest_client_timeout=15,
        storage_client_timeout=30,
        schema="public",
    )
    client = create_client(url, service_role_key, options=opts)
    return SupabaseHandle(client=client, bucket=bucket, table=table)


def upload_jpeg(
    sb: SupabaseHandle,
    storage_path: str,
    local_file_path: str,
    cache_control_seconds: int,
    upsert: bool,
) -> str:
    """
    Uploads a file to Supabase Storage.

    Returns the uploaded path (as reported by the SDK) or the intended path.
    """
    file_options = {
        "cache-control": str(cache_control_seconds),
        "upsert": "true" if upsert else "false",
        "content-type": "image/jpeg",
    }

    with open(local_file_path, "rb") as f:
        resp = sb.client.storage.from_(sb.bucket).upload(
            file=f,
            path=storage_path,
            file_options=file_options,
        )

    # The SDK commonly returns a response with .path. Be defensive.
    if hasattr(resp, "path") and resp.path:
        return resp.path  # type: ignore[attr-defined]

    if isinstance(resp, dict):
        return resp.get("path") or storage_path

    return storage_path


def upsert_still_row(
    sb: SupabaseHandle,
    row: Dict[str, Any],
    on_conflict: str,
) -> None:
    resp = sb.client.table(sb.table).upsert(row, on_conflict=on_conflict).execute()

    err = getattr(resp, "error", None)
    if err:
        raise RuntimeError(str(err))
```

#### `camera_pipeline/uploader.py`
```py
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
```

#### `camera_pipeline/scheduler.py`
```py
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
```

#### `camera_pipeline/logging_setup.py`
```py
import logging
import sys


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        fmt="%(asctime)sZ level=%(levelname)s logger=%(name)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    h.setFormatter(fmt)
    root.addHandler(h)
```

#### `camera_pipeline/main.py`
```py
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
        # We’ll just start uploader drain after a short capture loop.
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
```

## Step-by-step deployment on the Raspberry Pi

### Prepare Raspberry Pi OS and SSH access
1. Install Raspberry Pi OS using Raspberry Pi Imager. Raspberry Pi documentation describes Raspberry Pi OS availability and that it is distributed via Raspberry Pi Imager (including Lite for headless setups). citeturn3search2turn3search11  
2. Configure headless SSH access (recommended):
   - Raspberry Pi docs cover remote access and SSH key setup. citeturn3search0

### Configure the camera for RTSP
1. In the Reolink client/app, ensure RTSP is enabled and confirm the RTSP port. Reolink notes that RTSP/HTTP/ONVIF ports may be disabled by default and must be enabled in settings. citeturn4view6  
2. Verify the RTSP stream from a laptop on the same LAN using VLC:
   - Reolink provides RTSP URL patterns such as:
     - `rtsp://admin:password@ip_address/Preview_01_main`
     - `rtsp://admin:password@ip_address/Preview_01_sub` citeturn4view5  
   - If that path doesn’t work, try the alternate vendor-documented `h264Preview_01_main` form. citeturn0search4  
3. Avoid special characters in the password if you have RTSP auth issues (vendor note). citeturn4view5

### Create a restricted service user and directories
Run on the Pi:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin camera-pipeline

sudo mkdir -p /opt/camera-pipeline
sudo mkdir -p /etc/camera-pipeline
sudo mkdir -p /var/lib/camera-pipeline

sudo chown -R root:root /opt/camera-pipeline /etc/camera-pipeline
sudo chown -R camera-pipeline:camera-pipeline /var/lib/camera-pipeline
sudo chmod 750 /var/lib/camera-pipeline
```

### Install OS packages and Python venv
```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3 python3-venv git
```

### Install the application
```bash
sudo git clone YOUR_REPO_URL /opt/camera-pipeline
sudo chown -R root:root /opt/camera-pipeline

sudo python3 -m venv /opt/camera-pipeline/venv
sudo /opt/camera-pipeline/venv/bin/pip install --upgrade pip
sudo /opt/camera-pipeline/venv/bin/pip install -r /opt/camera-pipeline/requirements.txt
```

### Configure Supabase secrets and app config
1. Copy config and env templates:

```bash
sudo cp /opt/camera-pipeline/config.example.toml /etc/camera-pipeline/config.toml
sudo cp /opt/camera-pipeline/camera-pipeline.env.example /etc/camera-pipeline/camera-pipeline.env
sudo chmod 600 /etc/camera-pipeline/camera-pipeline.env
sudo chown root:root /etc/camera-pipeline/config.toml /etc/camera-pipeline/camera-pipeline.env
```

2. Edit `/etc/camera-pipeline/camera-pipeline.env` and set:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`  
  Supabase documents that service role keys bypass RLS and must not be exposed publicly. citeturn2search0turn4view3  
- `CAMERA_RTSP_URL`

### Validate Supabase project configuration
- Verify the `camera_stills` table exists with `UNIQUE(camera_id, capture_slot)`; Supabase upsert needs a UNIQUE constraint for the specified conflict columns. citeturn4view2turn12search29  
- Verify the `camera-stills` bucket exists and is private by default unless you chose public. citeturn11view1

### Run a one-shot test
```bash
sudo -u camera-pipeline -E /opt/camera-pipeline/venv/bin/python \
  -m camera_pipeline.main --config /etc/camera-pipeline/config.toml --once
```

If this succeeds, you should see:
- a JPEG briefly created under `/var/lib/camera-pipeline/pending/...`
- the object uploaded to Supabase Storage
- a row inserted/upserted in `camera_stills`

### Install and start as a systemd service
systemd supports a restart policy `Restart=on-failure` for services that exit with errors. citeturn6search28turn6search1  
Dependencies on `network-online.target` are commonly used to delay service startup until boot-time networking is ready. citeturn6search6turn6search2

```bash
sudo cp /opt/camera-pipeline/deploy/camera-pipeline.service /etc/systemd/system/camera-pipeline.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-pipeline
```

### View logs and health
- `journalctl` is the standard tool for viewing logs collected by systemd’s journal. citeturn6search3turn6search24  

```bash
journalctl -u camera-pipeline -f
```

Health file:
```bash
cat /var/lib/camera-pipeline/health.json
```

## Reliability, security posture, and failure modes

### Retry and idempotency mechanics
- **Idempotency**: `(camera_id, capture_slot)` uniquely identifies the expected still for each 30-second window. Postgres uniqueness constraints are enforced by unique indexes. citeturn12search1turn12search29  
- **DB upsert**: Supabase Python `upsert` supports `on_conflict`, which is designed to work with UNIQUE constraints. citeturn4view2turn12search27  
- **Storage overwrite behavior**: if you upload to the same path, Supabase Storage default behavior is a `400 Asset Already Exists` unless upsert is enabled; Supabase documents this and warns about overwriting due to CDN propagation delays. citeturn11view2  
  For this pipeline, object keys are normally unique; overwrites should occur only on restarts or rare duplicate scheduling events.

### Common failure modes and how this design handles them
- **RTSP capture failure** (camera rebooting, Wi‑Fi drop, wrong RTSP path): capture logs an error and skips that tick; next tick proceeds. Camera-side RTSP URLs and port enablement steps are vendor-documented and should be validated first. citeturn4view5turn4view6  
- **Internet outage / Supabase downtime**: captures continue into local spool until disk limits are hit; uploader retries with exponential backoff.
- **DB insert failures** (transient): uploader keeps the file and retries the DB upsert until success.
- **Disk full**: scheduler enforces `max_pending_files` and `max_pending_gb` and will skip capture if the spool is full. This prevents total disk exhaustion.
- **Credential leak risk**: store all secrets in an env file with `chmod 600` and do not embed service role keys in code or client-side apps. Supabase explicitly warns that service keys bypass RLS and must not be shared publicly. citeturn2search0turn4view3turn4view4

### Remote access without exposing the camera
If you want remote admin access to the Pi without port-forwarding:
- Use SSH (Raspberry Pi documents SSH remote access patterns). citeturn3search0  
- A VPN overlay can reduce public exposure. For example, entity["company","Tailscale","vpn and networking company"] documents “Tailscale SSH” which manages authentication/authorization within a tailnet and claims port 22 only for traffic from the tailnet. citeturn18view0  

## Phase two extension points

### Continuous recording as segmented MP4
The same pattern (local spool → upload → DB index) can be extended to video segments by using FFmpeg to write fixed-duration segments (e.g., 60s MP4) to local disk and uploading them. Segment objects can be indexed in a `camera_segments` table keyed by `(camera_id, segment_start)`.

### Inference pipeline
Once still ingestion is stable:
- Add a job runner that pulls new DB rows, downloads the still (or reads from local spool pre-delete), runs inference, and writes results into a JSONB column (`labels`, `model_version`, `confidence`).
- For private buckets, serve images to admin tools using signed URLs generated server-side; Supabase documents signed URL creation in Python and notes that private bucket assets require signed URLs or authenticated downloads. citeturn4view1turn11view0turn11view1