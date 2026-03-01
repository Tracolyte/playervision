# PlayerVision Repository Context

A complete technical reference for every file, function, data structure, and network interaction in the camera-pipeline codebase.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Network Topology: Camera, Pi, and Supabase](#2-network-topology-camera-pi-and-supabase)
3. [Process Lifecycle: Startup to Steady State](#3-process-lifecycle-startup-to-steady-state)
4. [File-by-File Code Reference](#4-file-by-file-code-reference)
5. [Data Structures and Contracts](#5-data-structures-and-contracts)
6. [Cross-File Interaction Map](#6-cross-file-interaction-map)
7. [The Capture Path in Detail](#7-the-capture-path-in-detail)
8. [The Upload Path in Detail](#8-the-upload-path-in-detail)
9. [Retry, Backoff, and Idempotency Mechanics](#9-retry-backoff-and-idempotency-mechanics)
10. [Local Spool Filesystem Layout](#10-local-spool-filesystem-layout)
11. [Configuration and Environment Variables](#11-configuration-and-environment-variables)
12. [systemd Service Integration](#12-systemd-service-integration)
13. [Logging and Observability](#13-logging-and-observability)
14. [Failure Modes and Recovery](#14-failure-modes-and-recovery)

---

## 1. System Overview

The pipeline is a single Python process running on a Raspberry Pi. It has two threads:

- **Thread 1 — CaptureScheduler**: Wakes every 30 seconds (configurable), checks if the current time falls within operating hours, grabs one JPEG frame from the camera over RTSP using FFmpeg, and writes it plus a metadata file to a local spool directory.

- **Thread 2 — UploadWorker**: Continuously scans the spool directory for pending jobs. For each job it finds, it uploads the JPEG to Supabase Storage, then upserts a metadata row into Supabase Postgres. On success, it deletes the local files. On failure, it writes a retry timestamp with exponential backoff.

The spool directory is the decoupling point. The capture thread only writes to local disk. The upload thread only reads from local disk and writes to Supabase. This means the Pi keeps capturing even if the internet is down — it just accumulates files locally until connectivity returns.

---

## 2. Network Topology: Camera, Pi, and Supabase

### Physical layout

```
┌──────────────────────┐         ┌──────────────────────┐         ┌─────────────────────┐
│   Reolink E1 Pro     │  RTSP   │   Raspberry Pi       │  HTTPS  │   Supabase Cloud    │
│   192.168.1.162:554  │◄────────│   (playervision)     │────────►│   (xfcsixtjhncrp…)  │
│                      │  TCP    │                      │  TLS    │                     │
│   Wi-Fi / Ethernet   │         │   Wi-Fi / Ethernet   │         │   Storage + Postgres│
└──────────────────────┘         └──────────────────────┘         └─────────────────────┘
        LAN only                    LAN + Internet                     Internet
```

### Camera → Pi (RTSP over TCP)

The Reolink E1 Pro exposes an RTSP server on port 554. The Pi connects as a client. The protocol works as follows:

1. **TCP connection**: The Pi's FFmpeg process opens a TCP socket to `192.168.1.162:554`.
2. **RTSP handshake**: FFmpeg sends `DESCRIBE`, `SETUP`, and `PLAY` commands. The camera responds with SDP (Session Description Protocol) describing the H.264 video stream.
3. **RTP over TCP (interleaved)**: Because we use `-rtsp_transport tcp`, the H.264 RTP packets are multiplexed over the same TCP connection (no separate UDP ports). This is more reliable over Wi-Fi where UDP packets can be dropped.
4. **Single frame extraction**: FFmpeg decodes the H.264 stream until it has one complete frame (`-frames:v 1`), encodes that frame as JPEG (`-q:v 2` quality, `-f image2` muxer), writes it to disk, and terminates the RTSP session.
5. **Timeout**: If the camera doesn't respond within 7 seconds (`-timeout 7000000` microseconds), FFmpeg exits with a non-zero status and the scheduler logs a capture failure.

The RTSP URL format is:
```
rtsp://admin:playervision2026@192.168.1.162:554/Preview_01_sub
```

- `admin:playervision2026` — camera credentials (HTTP Digest auth over RTSP)
- `192.168.1.162:554` — camera IP and RTSP port
- `/Preview_01_sub` — the sub-stream endpoint (lower resolution/bandwidth than `Preview_01_main`)

The sub-stream is used because stills don't need 4K resolution, and the sub-stream puts less load on the camera and network.

### Pi → Supabase (HTTPS)

All communication with Supabase uses the `supabase-py` SDK over HTTPS (TLS 1.2+). The Pi authenticates using the **service role key**, which is a long-lived JWT that bypasses Row Level Security (RLS).

Two separate HTTPS endpoints are used:

1. **Storage API** (`POST /storage/v1/object/<bucket>/<path>`)
   - Uploads the JPEG binary as the request body
   - Headers include `Authorization: Bearer <service_role_key>`, `Content-Type: image/jpeg`, `Cache-Control`, and `x-upsert`
   - The SDK sends this as an HTTP/2 POST to `https://<project>.supabase.co/storage/v1/object/camera-stills/<path>`
   - On success: HTTP 200 with the stored object path
   - On conflict without upsert: HTTP 400 "Asset Already Exists"

2. **PostgREST API** (`POST /rest/v1/<table>?on_conflict=...`)
   - Sends a JSON object with the row data
   - Uses the `Prefer: resolution=merge-duplicates` header (set by the SDK's upsert method)
   - On success: HTTP 201 Created
   - On conflict: performs `UPDATE` on the matching `(camera_id, capture_slot)` row instead of inserting

Both requests have explicit timeouts configured in the SDK: 30 seconds for Storage, 15 seconds for PostgREST.

---

## 3. Process Lifecycle: Startup to Steady State

### What happens when `python -m camera_pipeline.main --config config.toml` runs:

```
main.py:main()
    │
    ├── setup_logging()              → configures structured logging to stdout
    ├── parse_args()                 → reads --config path and --once flag
    ├── load_config(path)            → parses TOML, expands ${ENV_VARS}, returns AppConfig
    ├── init_spool(root_dir)         → creates /var/lib/camera-pipeline/pending/, returns SpoolPaths
    ├── init_supabase(url, key, …)   → creates Supabase client with timeouts, returns SupabaseHandle
    │
    ├── CaptureScheduler(…)          → constructed with all config values
    ├── UploadWorker(…)              → constructed with spool paths + Supabase handle
    │
    ├── Thread(scheduler.run_forever)  → started as daemon thread
    ├── Thread(uploader.run_forever)   → started as daemon thread
    │
    └── .join() on both threads      → main thread blocks forever (or 35s if --once)
```

Both threads are daemon threads, meaning they die when the main thread exits. In normal operation, `t_cap.join()` blocks forever because `run_forever()` never returns. If the process receives SIGTERM (from systemd stop), Python's default handler raises `SystemExit`, the main thread unblocks, and the daemon threads are killed.

---

## 4. File-by-File Code Reference

### `camera_pipeline/__init__.py`

Empty package marker. Contains `__all__ = []`. Exists solely so Python treats `camera_pipeline/` as an importable package.

---

### `camera_pipeline/main.py`

**Purpose**: Entry point. Wires everything together and starts the two threads.

**Functions**:

- `parse_args() -> argparse.Namespace`
  - Defines two CLI arguments: `--config` (required, path to TOML file) and `--once` (optional flag for testing)

- `main() -> int`
  - Calls `setup_logging()` to initialize structured logging
  - Calls `load_config(args.config)` to parse the TOML config with env var expansion
  - Calls `init_spool(cfg.spool.root_dir)` to create the spool directory structure
  - Calls `init_supabase(...)` to create the Supabase client
  - Constructs a `CaptureScheduler` dataclass with all capture-related config
  - Constructs an `UploadWorker` dataclass with spool paths and Supabase handle
  - If `--once`: starts both as daemon threads, sleeps 35 seconds, then returns 0
  - Otherwise: starts both as daemon threads, then joins them (blocks forever)

**Key detail**: The `--once` mode runs for exactly 35 seconds (slightly longer than one 30-second interval) to allow one capture cycle plus one upload cycle.

**Imports from other modules**:
- `config.load_config` — parses TOML
- `logging_setup.setup_logging` — configures logging
- `spool.init_spool` — creates directory structure
- `supabase_io.init_supabase` — creates Supabase client
- `scheduler.CaptureScheduler` — capture loop
- `uploader.UploadWorker` — upload loop

---

### `camera_pipeline/config.py`

**Purpose**: Loads and validates the TOML configuration file, expanding `${ENV_VAR}` references.

**Private functions**:

- `_expand_env(value: str) -> str`
  - Scans a string for `${VARNAME}` patterns
  - Looks up each variable in `os.environ`
  - Raises `RuntimeError` if a referenced variable is not set
  - Replaces the `${...}` token with the environment value
  - Loops to handle multiple variables in one string (e.g., `${A}/${B}`)

- `_expand_env_in_obj(obj: Any) -> Any`
  - Recursively walks the parsed TOML structure (dicts, lists, strings)
  - Applies `_expand_env` to every string value
  - Leaves non-string values (int, bool, float) untouched

- `_parse_hhmm(s: str) -> time`
  - Parses `"HH:MM"` strings into `datetime.time` objects
  - Used for operating hours start/end

**Dataclasses** (all frozen/immutable):

- `CameraConfig` — `camera_id: str`, `rtsp_url: str`
- `CaptureConfig` — `interval_seconds`, `rtsp_transport`, `ffmpeg_stimeout_us`, `jpeg_quality`, `scale_width`
- `OperatingHoursConfig` — `timezone`, `start`, `end`, `days`
- `SpoolConfig` — `root_dir`, `max_pending_files`, `max_pending_gb`, `delete_after_success`
- `SupabaseConfig` — `url`, `service_role_key`, `bucket`, `table`, `prefix`, `partition_timezone`, `storage_upsert`, `cache_control_seconds`
- `AppConfig` — aggregates all five config sections above

**Public function**:

- `load_config(path: str) -> AppConfig`
  - Opens the TOML file in binary mode (required by tomllib)
  - Parses it into a nested dict
  - Runs `_expand_env_in_obj` to substitute all `${...}` references
  - Constructs each frozen dataclass from the expanded dict
  - Casts numeric values with `int()` (TOML may parse them as int already, but this is defensive)
  - Lowercases day names for consistent matching

**TOML library**: Uses `tomllib` (Python 3.11+ stdlib). Falls back to `tomli` (pip package) on Python 3.9/3.10.

---

### `camera_pipeline/timeutil.py`

**Purpose**: All time-related utilities — current time, timezone conversion, operating hours checks, slot math, and interval alignment.

**Constants**:

- `DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]`
  - Indexed by Python's `weekday()` (Monday=0, Sunday=6)

**Functions**:

- `now_utc() -> datetime`
  - Returns `datetime.now(timezone.utc)` — timezone-aware UTC datetime

- `to_tz(dt_utc: datetime, tz_name: str) -> datetime`
  - Converts a UTC datetime to a named timezone using `zoneinfo.ZoneInfo`
  - Example: `to_tz(dt, "America/New_York")` converts UTC to Eastern

- `is_day_enabled(local_dt: datetime, enabled_days: List[str]) -> bool`
  - Maps the local datetime's weekday to a three-letter day name
  - Returns True if that day name is in the enabled list

- `within_hours(local_dt: datetime, start: time, end: time) -> bool`
  - Extracts the local time-of-day (stripping timezone info for comparison)
  - If `start < end` (normal window like 06:00–22:00): returns `start <= t < end`
  - If `start > end` (midnight-crossing window like 22:00–02:00): returns `t >= start OR t < end`
  - The end time is exclusive (a capture at exactly 22:00:00 would NOT fire if end is 22:00)

- `next_boundary(dt_utc: datetime, interval_seconds: int) -> datetime`
  - Computes the next interval-aligned UTC timestamp
  - Example: if interval is 30 and current epoch is 1000047, next boundary is 1000050
  - Formula: `((epoch // interval) + 1) * interval`
  - Returns a timezone-aware UTC datetime

- `capture_slot(dt_utc: datetime, interval_seconds: int) -> int`
  - Returns `floor(unix_epoch / interval_seconds)`
  - This integer uniquely identifies a 30-second window across all of time
  - Used as the idempotency key in Postgres

- `slot_start_utc(slot: int, interval_seconds: int) -> datetime`
  - Inverse of `capture_slot`: converts a slot number back to a UTC datetime
  - `slot * interval_seconds` gives the epoch second of the slot start

---

### `camera_pipeline/capture.py`

**Purpose**: Shells out to FFmpeg to grab a single JPEG frame from the RTSP stream.

**Dataclass**:

- `CaptureResult(frozen=True)` — `bytes: int`, `width: int`, `height: int`, `sha256: str`
  - Returned after a successful capture with metadata about the captured image

**Private functions**:

- `_run_ffmpeg_grab(rtsp_url, out_path, rtsp_transport, stimeout_us, jpeg_quality, scale_width) -> None`
  - Creates the output directory if needed (`out_path.parent.mkdir(parents=True)`)
  - Builds the FFmpeg command:
    ```
    ffmpeg -hide_banner -loglevel error
           -rtsp_transport tcp
           -timeout 7000000
           -i rtsp://admin:pass@ip/Preview_01_sub
           -an
           -frames:v 1
           -q:v 2
           [-vf scale=WIDTH:-2]
           -f image2 -y /path/to/still.jpg
    ```
  - Flag breakdown:
    - `-hide_banner`: suppress FFmpeg version info
    - `-loglevel error`: only print errors (reduces log noise)
    - `-rtsp_transport tcp`: use TCP interleaved transport (not UDP)
    - `-timeout 7000000`: RTSP I/O timeout in microseconds (7 seconds)
    - `-i <url>`: input RTSP stream
    - `-an`: disable audio processing
    - `-frames:v 1`: capture exactly 1 video frame then stop
    - `-q:v 2`: JPEG quality (2 = high quality, range 2–31, lower is better)
    - `-vf scale=W:-2`: optional downscale, `-2` auto-calculates height preserving aspect ratio and ensuring even pixel count
    - `-f image2`: force the image2 muxer (single image output)
    - `-y`: overwrite output file without asking
  - Calls `subprocess.run(cmd, check=True)` — raises `CalledProcessError` on non-zero exit

- `_sha256_file(path: Path) -> str`
  - Reads the file in 1MB chunks and computes SHA-256
  - Returns the hex digest string

**Public function**:

- `capture_still(rtsp_url, out_path, rtsp_transport, stimeout_us, jpeg_quality, scale_width) -> CaptureResult`
  - Calls `_run_ffmpeg_grab` to write the JPEG
  - Reads file size with `out_path.stat().st_size`
  - Opens the JPEG with Pillow to read dimensions (`im.size` returns `(width, height)`)
  - Computes SHA-256 hash
  - Returns a `CaptureResult` with all four values

---

### `camera_pipeline/storage_path.py`

**Purpose**: Builds deterministic Supabase Storage object keys from slot numbers.

**Functions**:

- `build_storage_path(prefix, camera_id, slot, interval_seconds, partition_timezone, local_tz_name) -> str`
  - Converts the slot number to a UTC datetime via `slot_start_utc`
  - If `partition_timezone == "local"`: converts to the configured timezone for date folder partitioning
  - If `partition_timezone == "utc"`: uses UTC for date folders
  - Extracts year, month, day strings from the (possibly local) datetime
  - Builds the filename using the UTC slot timestamp: `gym_cam_1_20260301T050900Z.jpg`
  - Returns: `stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg`
  - The date folders follow local timezone (so images captured at 11pm ET on March 1st go in the March 1st folder, not March 2nd UTC)
  - The filename always uses UTC (for global uniqueness)

- `storage_rel_dir(storage_path: str) -> str`
  - Strips the filename, returning just the directory portion
  - Example: `stills/gym_cam_1/2026/03/01/file.jpg` → `stills/gym_cam_1/2026/03/01`
  - Used to create matching local spool directories

---

### `camera_pipeline/spool.py`

**Purpose**: Manages the local filesystem queue (spool) where captured images wait for upload.

**Dataclass**:

- `SpoolPaths(frozen=True)` — `root: Path`, `pending: Path`, `health: Path`
  - `root` = `/var/lib/camera-pipeline`
  - `pending` = `/var/lib/camera-pipeline/pending`
  - `health` = `/var/lib/camera-pipeline/health.json`

**Functions**:

- `init_spool(root_dir: str) -> SpoolPaths`
  - Creates `pending/` subdirectory and root directory with `mkdir(parents=True, exist_ok=True)`
  - Returns the three paths as a frozen dataclass

- `_atomic_write_json(path: Path, payload: Dict) -> None`
  - Writes JSON to a `.tmp` file first
  - Calls `f.flush()` then `os.fsync(f.fileno())` to force data to disk
  - Atomically renames `.tmp` → target path with `tmp.replace(path)`
  - This prevents corrupted JSON if the Pi loses power mid-write

- `write_health(health_path: Path, payload: Dict) -> None`
  - Thin wrapper around `_atomic_write_json`

- `pending_job_paths(pending_root: Path) -> Iterator[Path]`
  - Recursively finds all `job.json` files under `pending/`
  - Uses `Path.rglob("job.json")`
  - Yields paths lazily (iterator, not list)

- `job_dir_for(pending_root: Path, storage_rel_dir: str) -> Path`
  - Joins the pending root with a storage-relative directory path
  - Example: `/var/lib/camera-pipeline/pending` + `stills/gym_cam_1/2026/03/01`

- `bytes_in_tree(path: Path) -> int`
  - Recursively sums the size of all files under a path
  - Used for backpressure calculations

- `count_pending_jobs(pending_root: Path) -> int`
  - Counts all `job.json` files using `pending_job_paths`
  - Used for health reporting

- `remove_tree_if_empty(path: Path, stop_at: Path) -> None`
  - Walks up from `path` to `stop_at`, removing each directory if empty
  - Stops on first `OSError` (directory not empty) or when reaching `stop_at`

- `delete_job_artifacts(job_path: Path) -> None`
  - Deletes `still.jpg` and `job.json` from the job directory
  - Calls `remove_tree_if_empty` to clean up empty parent directories
  - Note: The uploader uses its own inline cleanup rather than this function

---

### `camera_pipeline/supabase_io.py`

**Purpose**: All Supabase interactions — client initialization, Storage upload, and Postgres upsert.

**Dataclass**:

- `SupabaseHandle(frozen=True)` — `client: Client`, `bucket: str`, `table: str`
  - Holds the initialized Supabase client and target bucket/table names

**Functions**:

- `init_supabase(url, service_role_key, bucket, table) -> SupabaseHandle`
  - Creates `ClientOptions` with explicit timeouts:
    - `postgrest_client_timeout=15` — 15-second timeout for database operations
    - `storage_client_timeout=30` — 30-second timeout for file uploads
    - `schema="public"` — target the public schema
  - Calls `create_client(url, service_role_key, options=opts)`
  - The service role key is passed as the second argument, which the SDK uses as the `Authorization: Bearer` token
  - This key bypasses all RLS policies — the Pi is a trusted server, not a browser client

- `upload_jpeg(sb, storage_path, local_file_path, cache_control_seconds, upsert) -> str`
  - Constructs `file_options` dict:
    - `cache-control`: string seconds (e.g., `"3600"`)
    - `upsert`: `"true"` or `"false"` — if true, overwrites existing objects at the same path
    - `content-type`: `"image/jpeg"`
  - Opens the local JPEG file in binary mode
  - Calls `sb.client.storage.from_(sb.bucket).upload(file=f, path=storage_path, file_options=file_options)`
  - Under the hood, the SDK makes an HTTP POST to `https://<project>.supabase.co/storage/v1/object/<bucket>/<path>` with the file bytes as the body
  - Defensively extracts the returned path from the response (handles different SDK response shapes)
  - Returns the storage path string

- `upsert_still_row(sb, row, on_conflict) -> None`
  - Calls `sb.client.table(sb.table).upsert(row, on_conflict=on_conflict).execute()`
  - `on_conflict="camera_id,capture_slot"` tells PostgREST to use `INSERT ... ON CONFLICT (camera_id, capture_slot) DO UPDATE`
  - The SDK sends a POST to `https://<project>.supabase.co/rest/v1/camera_stills?on_conflict=camera_id,capture_slot` with `Prefer: resolution=merge-duplicates`
  - Checks for errors on the response and raises `RuntimeError` if present

---

### `camera_pipeline/uploader.py`

**Purpose**: The upload worker thread. Drains the local spool by uploading to Supabase.

**Private helper functions**:

- `_utc_now() -> datetime` — same as `timeutil.now_utc()` but local to this module
- `_parse_dt(s: str) -> datetime` — parses ISO 8601 strings, replacing `Z` with `+00:00` for Python compatibility
- `_to_iso_z(dt: datetime) -> str` — converts datetime to ISO 8601 with `Z` suffix
- `_backoff_seconds(attempt, base=5, cap=300) -> int` — exponential backoff: `min(300, 5 * 2^(attempt-1))`
  - Attempt 1: 5s, attempt 2: 10s, attempt 3: 20s, attempt 4: 40s, ..., capped at 300s (5 minutes)

**Class: `UploadWorker`** (mutable dataclass):

Fields:
- `pending_root: Path` — `/var/lib/camera-pipeline/pending`
- `health_path: Path` — `/var/lib/camera-pipeline/health.json`
- `sb: SupabaseHandle` — the Supabase client
- `on_conflict: str` — `"camera_id,capture_slot"`
- `delete_after_success: bool` — whether to delete local files after upload
- `cache_control_seconds: int` — Cache-Control header value for uploads
- `storage_upsert: bool` — whether to allow overwriting in Storage

Methods:

- `run_forever(self) -> None`
  - Infinite loop: calls `_drain_once()`, sleeps 2 seconds if no work was done
  - The 2-second sleep prevents busy-spinning when the spool is empty

- `_drain_once(self) -> bool`
  - Lists all `job.json` files in the spool (sorted for deterministic order)
  - For each job: reads the JSON, checks `next_attempt_at` against current time
  - If `next_attempt_at` is in the future, skips (job is in backoff)
  - Processes the first eligible job, writes health, returns True
  - If no eligible jobs found, writes health, returns False
  - Only processes ONE job per drain cycle (then the outer loop calls again)

- `_process_job(self, job_path, job) -> bool`
  - Checks that `still.jpg` exists alongside the `job.json`
  - Increments `attempt` counter and records `last_attempt_at`
  - **Step 1**: Upload JPEG to Supabase Storage
    - On failure: computes backoff delay, writes `next_attempt_at` to job file, returns False
  - **Step 2**: Upsert metadata row to Postgres (only runs if Step 1 succeeded)
    - Builds a row dict with: `camera_id`, `capture_slot`, `captured_at`, `storage_bucket`, `storage_path`, `bytes`, `width`, `height`, `sha256`, `status="indexed"`, `error=None`
    - On failure: computes backoff delay, writes `next_attempt_at` to job file, returns False
  - **Step 3**: Cleanup (only runs if both steps succeeded)
    - Deletes `still.jpg` and `job.json`
    - Walks up the directory tree removing empty parent directories (stops at `pending_root`)
  - Logs `job_success` and returns True

- `_cleanup_empty_parents(self, start_dir) -> None`
  - Walks from `start_dir` up to `pending_root`, calling `rmdir()` on each
  - Stops on the first `OSError` (directory not empty) or when reaching `pending_root`

- `_atomic_job_write(self, job_path, job) -> None`
  - Writes JSON to `.json.tmp` then renames to `.json`
  - No `fsync` here (unlike `spool._atomic_write_json`) — acceptable because losing a retry state on power loss just means an extra retry

- `_write_health(self) -> None`
  - Writes `health.json` with: `ts` (current UTC), `pending_jobs` (count), `pending_bytes` (total spool size)
  - Catches and logs exceptions (health is best-effort)

---

### `camera_pipeline/scheduler.py`

**Purpose**: The capture scheduler thread. Time-gates captures and writes to the spool.

**Private function**:

- `_to_iso_z(dt) -> str` — converts datetime to ISO 8601 with `Z` suffix

**Class: `CaptureScheduler`** (mutable dataclass):

Fields (all set from config at construction):
- `pending_root`, `camera_id`, `rtsp_url`
- `interval_seconds`, `rtsp_transport`, `ffmpeg_stimeout_us`, `jpeg_quality`, `scale_width`
- `tz_name`, `start_time`, `end_time`, `days`
- `storage_bucket`, `storage_prefix`, `partition_timezone`
- `max_pending_files`, `max_pending_gb`
- `delete_after_success` (not used in scheduler, kept for symmetry with uploader)

Methods:

- `run_forever(self) -> None`
  - Infinite loop with this logic per iteration:
    1. Get current UTC time and convert to local timezone
    2. Check `is_day_enabled` and `within_hours` — if outside operating hours, sleep 30s and restart loop
    3. Compute `next_boundary` — the next interval-aligned UTC timestamp
    4. Sleep until that boundary (could be 0–30 seconds)
    5. Re-check operating hours (in case we slept past the end of hours)
    6. Compute `capture_slot` for the current time
    7. Check `_spool_has_capacity` — if spool is full, log warning and skip
    8. Call `_capture_to_spool(slot)`

- `_spool_has_capacity(self) -> bool`
  - Counts `job.json` files with early exit: if count exceeds `max_pending_files`, returns False immediately
  - Sums total bytes of all files in spool, compares against `max_pending_gb * 1024^3`
  - Returns True only if both limits are within bounds

- `_capture_to_spool(self, slot) -> None`
  - Builds the Supabase storage path from the slot number (via `build_storage_path`)
  - Derives the local spool directory from the storage path (mirrors the same folder structure)
  - **Dedup check**: if `job.json` AND `still.jpg` already exist for this slot, logs `slot_already_queued` and returns (prevents re-capturing the same 30-second window)
  - Calls `capture_still(...)` to run FFmpeg and get a `CaptureResult`
  - On FFmpeg failure: logs `capture_failed` and returns (no crash, next tick will try again)
  - On success: constructs a `job` dict with all metadata:
    - `camera_id`, `capture_slot`, `captured_at` (slot start as ISO string)
    - `storage_bucket`, `storage_path` (where the file will go in Supabase)
    - `bytes`, `width`, `height`, `sha256` (from `CaptureResult`)
    - `attempt: 0`, `next_attempt_at` (now — eligible for immediate upload)
  - Writes `job.json` atomically (`.json.tmp` → `.json` rename)
  - Logs `capture_ok` with slot number, file size, and storage path

---

### `camera_pipeline/logging_setup.py`

**Purpose**: Configures Python's logging module for structured output.

**Function**:

- `setup_logging() -> None`
  - Sets root logger to `INFO` level
  - Adds a `StreamHandler` writing to `sys.stdout`
  - Format: `2026-03-01T00:09:04Z level=INFO logger=camera_pipeline.scheduler msg=capture_ok ...`
  - Date format: `%Y-%m-%dT%H:%M:%S` with `Z` suffix appended by the format string
  - All logs go to stdout, which systemd's journal captures automatically

---

### `config.example.toml`

The template configuration file. See section 11 for full documentation.

---

### `camera-pipeline.env.example`

Template for the secrets file. Contains `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `CAMERA_RTSP_URL`.

---

### `deploy/camera-pipeline.service`

The systemd unit file. See section 12 for full documentation.

---

### `requirements.txt`

```
supabase==2.28.0       — Supabase Python SDK (includes httpx, postgrest, storage3, gotrue)
Pillow==10.4.0         — Image library, used only for reading JPEG width/height
tomli==2.0.1           — TOML parser, only installed on Python < 3.11
```

---

## 5. Data Structures and Contracts

### job.json (local spool file)

Written by the scheduler, read and updated by the uploader.

```json
{
  "attempt": 0,
  "bytes": 62619,
  "camera_id": "gym_cam_1",
  "capture_slot": 59078058,
  "captured_at": "2026-03-01T05:09:00Z",
  "height": 1080,
  "next_attempt_at": "2026-03-01T05:09:04Z",
  "sha256": "a1b2c3...",
  "storage_bucket": "camera-stills",
  "storage_path": "stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg",
  "width": 1920
}
```

After a failed upload attempt, additional fields appear:
```json
{
  "attempt": 2,
  "upload_status": "error",
  "error": "upload_failed: HTTPStatusError 500",
  "last_attempt_at": "2026-03-01T05:09:40Z",
  "next_attempt_at": "2026-03-01T05:09:50Z"
}
```

After successful upload + DB upsert:
```json
{
  "upload_status": "ok",
  "uploaded_path": "stills/gym_cam_1/...",
  "db_status": "ok"
}
```
(This state is transient — the file is deleted immediately after.)

### health.json (observability file)

Written by the uploader after every drain cycle.

```json
{
  "pending_bytes": 125238,
  "pending_jobs": 2,
  "ts": "2026-03-01T05:10:06Z"
}
```

### Postgres row (camera_stills table)

```
id:              uuid (auto-generated)
camera_id:       "gym_cam_1"
capture_slot:    59078058
captured_at:     2026-03-01T05:09:00+00:00
storage_bucket:  "camera-stills"
storage_path:    "stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg"
bytes:           62619
width:           1920
height:          1080
sha256:          "a1b2c3..."
status:          "indexed"
error:           null
created_at:      2026-03-01T05:09:06+00:00
```

### Supabase Storage object

```
Bucket:  camera-stills
Path:    stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg
Type:    image/jpeg
Size:    ~60KB per frame (sub-stream)
```

---

## 6. Cross-File Interaction Map

```
main.py
  ├── imports config.py          → load_config()
  ├── imports logging_setup.py   → setup_logging()
  ├── imports spool.py           → init_spool()
  ├── imports supabase_io.py     → init_supabase()
  ├── imports scheduler.py       → CaptureScheduler
  └── imports uploader.py        → UploadWorker

scheduler.py
  ├── imports capture.py         → capture_still()
  ├── imports storage_path.py    → build_storage_path(), storage_rel_dir()
  └── imports timeutil.py        → now_utc(), to_tz(), is_day_enabled(),
                                    within_hours(), next_boundary(),
                                    capture_slot(), slot_start_utc()

uploader.py
  ├── imports spool.py           → pending_job_paths(), write_health(),
  │                                 count_pending_jobs(), bytes_in_tree()
  └── imports supabase_io.py     → SupabaseHandle, upload_jpeg(), upsert_still_row()

storage_path.py
  └── imports timeutil.py        → slot_start_utc()

capture.py
  └── imports PIL.Image          → reading JPEG dimensions (external dep)

supabase_io.py
  └── imports supabase           → create_client, Client, ClientOptions (external dep)
```

The two thread classes (`CaptureScheduler` and `UploadWorker`) never import each other and never share objects at runtime. Their only shared state is the filesystem: the `pending/` directory. The scheduler writes files, the uploader reads and deletes them.

---

## 7. The Capture Path in Detail

This is the complete sequence when the scheduler fires a capture:

```
1. scheduler.run_forever() wakes at interval boundary
2. Checks: is today an enabled day? Is current local time within hours?
3. Computes slot = floor(unix_epoch / 30)
4. Checks spool capacity (file count + disk usage)
5. Calls _capture_to_spool(slot)
    │
    ├── build_storage_path(prefix, camera_id, slot, …)
    │     └── Returns: "stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg"
    │
    ├── storage_rel_dir(storage_path)
    │     └── Returns: "stills/gym_cam_1/2026/03/01"
    │
    ├── Dedup check: job.json + still.jpg already exist? → skip
    │
    ├── capture_still(rtsp_url, out_path, …)
    │     │
    │     ├── _run_ffmpeg_grab(…)
    │     │     └── subprocess.run(["ffmpeg", "-rtsp_transport", "tcp",
    │     │           "-timeout", "7000000", "-i", "rtsp://…",
    │     │           "-frames:v", "1", "-q:v", "2",
    │     │           "-f", "image2", "-y", "/path/still.jpg"])
    │     │
    │     │     [Camera receives RTSP DESCRIBE/SETUP/PLAY]
    │     │     [Camera streams H.264 over TCP]
    │     │     [FFmpeg decodes one frame, encodes as JPEG, writes to disk]
    │     │     [FFmpeg sends RTSP TEARDOWN, closes TCP]
    │     │
    │     ├── stat() → file size in bytes
    │     ├── PIL Image.open() → width, height
    │     └── sha256 hash of file
    │
    ├── Construct job dict with all metadata
    ├── Atomic write: job.json.tmp → job.json
    └── Log: capture_ok
```

---

## 8. The Upload Path in Detail

This is the complete sequence when the uploader processes a job:

```
1. uploader.run_forever() wakes (every 2s or immediately after work)
2. _drain_once() lists and sorts all job.json files
3. For each job: reads JSON, checks next_attempt_at vs now
4. First eligible job → _process_job(job_path, job)
    │
    ├── Verify still.jpg exists alongside job.json
    ├── Increment attempt counter, record last_attempt_at
    │
    ├── STEP 1: Upload to Supabase Storage
    │     │
    │     └── upload_jpeg(sb, storage_path, local_file_path, …)
    │           │
    │           ├── Open local JPEG in binary mode
    │           ├── sb.client.storage.from_("camera-stills").upload(
    │           │     file=<bytes>, path="stills/…/file.jpg",
    │           │     file_options={cache-control, upsert, content-type})
    │           │
    │           │  [Pi opens TLS connection to Supabase]
    │           │  [POST /storage/v1/object/camera-stills/stills/…/file.jpg]
    │           │  [Request body: raw JPEG bytes]
    │           │  [Response: 200 OK with path]
    │           │
    │           └── Returns uploaded path string
    │
    │     On failure → write backoff to job.json, return False
    │
    ├── STEP 2: Upsert to Supabase Postgres (only if Step 1 succeeded)
    │     │
    │     └── upsert_still_row(sb, row, on_conflict="camera_id,capture_slot")
    │           │
    │           ├── sb.client.table("camera_stills").upsert(row, on_conflict=…).execute()
    │           │
    │           │  [POST /rest/v1/camera_stills?on_conflict=camera_id,capture_slot]
    │           │  [Prefer: resolution=merge-duplicates]
    │           │  [Body: JSON row data]
    │           │  [Response: 201 Created]
    │           │
    │           │  Postgres executes:
    │           │    INSERT INTO camera_stills (camera_id, capture_slot, …)
    │           │    VALUES ('gym_cam_1', 59078058, …)
    │           │    ON CONFLICT (camera_id, capture_slot)
    │           │    DO UPDATE SET …
    │           │
    │           └── Checks response for errors
    │
    │     On failure → write backoff to job.json, return False
    │
    ├── STEP 3: Cleanup (only if both steps succeeded)
    │     ├── Delete still.jpg
    │     ├── Delete job.json
    │     └── Remove empty parent directories up to pending_root
    │
    └── Log: job_success
```

---

## 9. Retry, Backoff, and Idempotency Mechanics

### Retry with exponential backoff

When an upload or DB upsert fails:

1. The `attempt` counter in `job.json` is incremented
2. A backoff delay is computed: `min(300, 5 * 2^(attempt-1))`
   - Attempt 1 → 5 seconds
   - Attempt 2 → 10 seconds
   - Attempt 3 → 20 seconds
   - Attempt 4 → 40 seconds
   - Attempt 5 → 80 seconds
   - Attempt 6 → 160 seconds
   - Attempt 7+ → 300 seconds (5-minute cap)
3. `next_attempt_at` is set to `now + delay`
4. The updated job is written atomically to disk
5. On the next drain cycle, the uploader skips this job until `next_attempt_at` has passed

There is no maximum attempt limit. Jobs retry indefinitely until they succeed or are manually deleted.

### Idempotency guarantees

**Storage**: If `storage_upsert = true`, uploading the same path twice overwrites the object. The JPEG content is identical (same capture), so this is safe.

**Postgres**: The `UNIQUE(camera_id, capture_slot)` constraint combined with `ON CONFLICT DO UPDATE` means:
- First insert: creates the row
- Repeat insert with same `(camera_id, capture_slot)`: updates the existing row with the new values
- This is safe because the values are deterministic for a given slot

**Capture dedup**: The scheduler checks if `job.json` AND `still.jpg` already exist for a slot before capturing. If both exist, it skips. This prevents re-running FFmpeg for a slot that's already queued.

### Ordering guarantee

The upload always happens before the DB upsert. This means:
- A DB row always points to a Storage object that exists (no orphaned DB rows)
- A Storage object may temporarily exist without a DB row (if the DB upsert fails), but the retry will eventually create the row

---

## 10. Local Spool Filesystem Layout

```
/var/lib/camera-pipeline/
├── health.json                              ← written by uploader every drain cycle
└── pending/                                 ← the job queue
    └── stills/
        └── gym_cam_1/
            └── 2026/
                └── 03/
                    └── 01/
                        ├── job.json         ← metadata for uploader
                        └── still.jpg        ← the captured JPEG
```

The directory structure under `pending/` mirrors the Supabase Storage path structure. This means:
- Each slot gets its own directory (because the filename is unique per slot)
- The uploader can derive the Storage path from the job metadata, not the filesystem path
- After successful upload, both files are deleted and empty parent directories are pruned

---

## 11. Configuration and Environment Variables

### config.toml sections

**`[camera]`**
| Key | Type | Description |
|-----|------|-------------|
| `camera_id` | string | Unique identifier for this camera (used in Storage paths and DB rows) |
| `rtsp_url` | string | RTSP URL with `${CAMERA_RTSP_URL}` env var reference |

**`[capture]`**
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `interval_seconds` | int | 30 | Seconds between captures |
| `rtsp_transport` | string | "tcp" | RTSP transport protocol ("tcp" or "udp") |
| `ffmpeg_stimeout_us` | int | 7000000 | FFmpeg RTSP timeout in microseconds |
| `jpeg_quality` | int | 2 | FFmpeg JPEG quality (2=best, 31=worst) |
| `scale_width` | int | 0 | Downscale width in pixels (0=disabled) |

**`[operating_hours]`**
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `timezone` | string | "America/New_York" | IANA timezone for schedule evaluation |
| `start` | string | "00:00" | Start time (HH:MM, inclusive) |
| `end` | string | "23:59" | End time (HH:MM, exclusive) |
| `days` | array | all 7 days | Enabled days (three-letter lowercase) |

**`[spool]`**
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `root_dir` | string | "/var/lib/camera-pipeline" | Root of the spool directory |
| `max_pending_files` | int | 20000 | Max queued jobs before capture stops |
| `max_pending_gb` | int | 10 | Max spool disk usage in GB before capture stops |
| `delete_after_success` | bool | true | Delete local files after successful upload |

**`[supabase]`**
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `url` | string | (env var) | Supabase project URL |
| `service_role_key` | string | (env var) | Supabase service role key |
| `bucket` | string | "camera-stills" | Storage bucket name |
| `table` | string | "camera_stills" | Postgres table name |
| `prefix` | string | "stills" | First path segment in Storage keys |
| `partition_timezone` | string | "local" | Date folders use "local" or "utc" timezone |
| `storage_upsert` | bool | true | Allow overwriting existing Storage objects |
| `cache_control_seconds` | int | 3600 | Cache-Control header on uploaded objects |

### Environment variables (in camera-pipeline.env)

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | `https://<project_ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Long JWT string from Supabase dashboard |
| `CAMERA_RTSP_URL` | Full RTSP URL including credentials |

The env file is loaded by systemd's `EnvironmentFile=` directive. The config loader's `_expand_env` function then substitutes `${VARNAME}` references in the TOML with the values from the process environment.

---

## 12. systemd Service Integration

### Unit file: `deploy/camera-pipeline.service`

```ini
[Unit]
Description=Camera still capture pipeline (RTSP -> Supabase Storage -> Postgres)
Wants=network-online.target
After=network-online.target
```
- Waits for network to be up before starting (important because the Pi connects to both the camera on LAN and Supabase on the internet)

```ini
[Service]
Type=simple
User=camera-pipeline
Group=camera-pipeline
EnvironmentFile=/etc/camera-pipeline/camera-pipeline.env
ExecStart=/opt/camera-pipeline/venv/bin/python -m camera_pipeline.main --config /etc/camera-pipeline/config.toml
WorkingDirectory=/opt/camera-pipeline
Restart=on-failure
RestartSec=5
```
- Runs as a dedicated unprivileged user (`camera-pipeline`)
- Loads secrets from the env file into the process environment
- Runs the Python module with the config path
- `WorkingDirectory` ensures Python can find the `camera_pipeline` package
- `Restart=on-failure` + `RestartSec=5`: if the process crashes, systemd restarts it after 5 seconds

```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/camera-pipeline
ReadOnlyPaths=/etc/camera-pipeline
```
- Security hardening:
  - Cannot escalate privileges
  - Gets its own `/tmp` (isolated from other processes)
  - Filesystem is read-only except for explicitly listed paths
  - Home directories are inaccessible
  - Spool directory is writable, config directory is read-only

---

## 13. Logging and Observability

### Log format

All logs go to stdout in structured format:
```
2026-03-01T00:09:04Z level=INFO logger=camera_pipeline.scheduler msg=capture_ok camera_id=gym_cam_1 slot=59078058 bytes=62619 storage_path=stills/…
```

systemd's journal captures stdout automatically. View with:
```bash
journalctl -u camera-pipeline -f        # live tail
journalctl -u camera-pipeline --since "1 hour ago"  # recent history
```

### Key log messages

| Logger | Level | Message | Meaning |
|--------|-------|---------|---------|
| `scheduler` | INFO | `capture_ok` | JPEG captured and spooled successfully |
| `scheduler` | INFO | `slot_already_queued` | Slot already has a pending job, skipped |
| `scheduler` | ERROR | `capture_failed` | FFmpeg returned non-zero (camera unreachable, timeout, etc.) |
| `scheduler` | WARNING | `spool_full` | Spool exceeded file count or disk limit |
| `uploader` | INFO | `job_success` | Upload + DB upsert both succeeded, files cleaned up |
| `uploader` | WARNING | `upload_failed` | Supabase Storage upload failed (with retry delay) |
| `uploader` | WARNING | `db_upsert_failed` | Postgres upsert failed (with retry delay) |
| `uploader` | ERROR | `missing_still` | job.json exists but still.jpg is missing |
| `uploader` | ERROR | `failed_to_read_job` | Corrupted job.json |
| `httpx` | INFO | `HTTP Request: POST …` | Raw HTTP request logs from the Supabase SDK |

### Health file

`/var/lib/camera-pipeline/health.json` is updated after every uploader drain cycle:
```json
{
  "ts": "2026-03-01T05:10:06Z",
  "pending_jobs": 0,
  "pending_bytes": 0
}
```
- `pending_jobs > 0` with `pending_bytes` growing means uploads are failing
- `ts` not updating means the uploader thread has stalled

---

## 14. Failure Modes and Recovery

| Failure | What Happens | Recovery |
|---------|-------------|----------|
| **Camera offline / RTSP timeout** | FFmpeg exits non-zero, scheduler logs `capture_failed`, skips this tick | Automatic — next tick tries again |
| **Wi-Fi drops during capture** | Same as above — FFmpeg timeout after 7s | Automatic |
| **Internet down** | Captures continue to spool locally. Uploader retries with backoff (5s → 300s cap) | Automatic — uploader drains backlog when internet returns |
| **Supabase 5xx error** | Upload or DB upsert fails, job stays in spool with backoff | Automatic retry |
| **Supabase rate limit** | Same as above | Automatic — backoff gives Supabase time to recover |
| **Disk full on Pi** | Scheduler checks `max_pending_files` and `max_pending_gb`, stops capturing when limits hit | Automatic — resumes when uploader drains some jobs |
| **Pi reboots** | systemd restarts the service (`enable --now`). Spool files survive reboot. Uploader resumes draining. | Automatic |
| **Process crash** | `Restart=on-failure` in systemd restarts after 5 seconds | Automatic |
| **Corrupted job.json** | Uploader logs `failed_to_read_job`, skips this file, continues with others | Manual cleanup needed |
| **Duplicate slot (restart mid-tick)** | Scheduler sees existing `job.json + still.jpg`, logs `slot_already_queued`, skips. DB upsert uses `ON CONFLICT DO UPDATE`. | Automatic — no duplicates |
| **Storage object already exists** | `storage_upsert=true` overwrites it. If false, returns 400 error and retries. | Automatic with upsert=true |
| **Power loss mid-write** | Atomic writes (`.tmp` → rename) mean files are either complete or absent. `fsync` on job writes ensures durability. | Automatic — no partial files |
