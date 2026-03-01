# PlayerVision Camera Pipeline — Implementation Report

## Status: MVP Complete & Verified on Production Hardware

**Date:** 2026-03-01
**Deployment target:** Raspberry Pi (hostname: `playervision`)
**Camera:** Reolink E1 Pro on local LAN
**Backend:** Supabase (Storage + Postgres)

---

## 1. What Was Built

A Python service that runs on a Raspberry Pi, captures a single JPEG still from an RTSP IP camera every 30 seconds, uploads it to Supabase Storage, and upserts a metadata row into Supabase Postgres. Two decoupled loops (capture + upload) ensure the Pi keeps capturing even during network outages, buffering locally until connectivity returns.

### Verified End-to-End on 2026-03-01

The following one-shot test succeeded on the Pi:

```
2026-03-01T00:09:04Z level=INFO logger=camera_pipeline.scheduler msg=capture_ok camera_id=gym_cam_1 slot=59078058 bytes=62619 storage_path=stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg
2026-03-01T00:09:05Z level=INFO logger=httpx msg=HTTP Request: POST https://xfcsixtjhncrpfztqvhf.supabase.co/storage/v1/object/camera-stills/stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg "HTTP/2 200 OK"
2026-03-01T00:09:06Z level=INFO logger=httpx msg=HTTP Request: POST https://xfcsixtjhncrpfztqvhf.supabase.co/rest/v1/camera_stills?on_conflict=camera_id%2Ccapture_slot "HTTP/2 201 Created"
2026-03-01T00:09:06Z level=INFO logger=camera_pipeline.uploader msg=job_success camera_id=gym_cam_1 slot=59078058 storage_path=stills/gym_cam_1/2026/03/01/gym_cam_1_20260301T050900Z.jpg
```

---

## 2. Repository Structure

```
playervision/
├── README.md
├── requirements.txt
├── config.example.toml
├── camera-pipeline.env.example
├── plan.md
├── report.md                        ← this file
├── deploy/
│   └── camera-pipeline.service      ← systemd unit
└── camera_pipeline/                 ← Python package
    ├── __init__.py
    ├── main.py                      ← entry point, thread orchestration
    ├── config.py                    ← TOML loading + env var expansion
    ├── timeutil.py                  ← timezone, slot math, scheduling
    ├── spool.py                     ← local disk queue management
    ├── storage_path.py              ← Supabase Storage key builder
    ├── capture.py                   ← FFmpeg RTSP frame grab
    ├── supabase_io.py               ← Storage upload + Postgres upsert
    ├── uploader.py                  ← upload worker with retry/backoff
    ├── scheduler.py                 ← time-gated capture loop
    └── logging_setup.py             ← structured logging to stdout
```

---

## 3. Architecture

### Two-loop design

| Loop | Thread | Responsibility |
|------|--------|---------------|
| **Capture scheduler** | `CaptureScheduler.run_forever()` | Every 30s (during operating hours), grab one JPEG via FFmpeg RTSP, write `still.jpg` + `job.json` to local spool |
| **Upload worker** | `UploadWorker.run_forever()` | Scan spool for eligible jobs, upload JPEG to Supabase Storage, upsert row to Postgres, delete local files on success |

The spool directory (`/var/lib/camera-pipeline/pending/`) is the decoupling point. If the internet is down, captures accumulate locally. When connectivity returns, the upload worker drains the backlog.

### Data flow

```
RTSP Camera
    │
    ▼ (FFmpeg -frames:v 1)
Local spool: /var/lib/camera-pipeline/pending/stills/<cam>/<YYYY>/<MM>/<DD>/
    ├── still.jpg
    └── job.json (metadata + retry state)
    │
    ▼ (Supabase Python SDK)
Supabase Storage: camera-stills bucket
    path: stills/<camera_id>/<YYYY>/<MM>/<DD>/<camera_id>_<slot_ts>.jpg
    │
    ▼ (upsert on camera_id, capture_slot)
Supabase Postgres: camera_stills table
```

### Idempotency

Each 30-second window maps to a `capture_slot = floor(unix_epoch / 30)`. The Postgres table has `UNIQUE(camera_id, capture_slot)`, and upserts use `ON CONFLICT` to avoid duplicate rows. Storage uploads use `upsert=true` to overwrite if the same slot is retried.

---

## 4. Infrastructure Already Configured

### On the Raspberry Pi (`playervision`)

| Item | Details |
|------|---------|
| OS | Raspberry Pi OS |
| FFmpeg | Installed via `apt-get install ffmpeg` |
| Python venv | `/opt/camera-pipeline/venv/` with `supabase==2.28.0`, `Pillow==10.4.0` |
| Service user | `camera-pipeline` (system user, no home, no login shell) |
| Application code | `/opt/camera-pipeline/` |
| Config file | `/etc/camera-pipeline/config.toml` |
| Secrets env file | `/etc/camera-pipeline/camera-pipeline.env` (chmod 640, owned `root:camera-pipeline`) |
| Spool directory | `/var/lib/camera-pipeline/` (owned `camera-pipeline:camera-pipeline`, mode 750) |
| systemd service | `/etc/systemd/system/camera-pipeline.service` |

### On Supabase

| Item | Details |
|------|---------|
| Project URL | `https://xfcsixtjhncrpfztqvhf.supabase.co` |
| Storage bucket | `camera-stills` (private) |
| Postgres table | `camera_stills` with `UNIQUE(camera_id, capture_slot)` |
| Auth | Service role key (bypasses RLS, stored only on Pi) |

### Camera

| Item | Details |
|------|---------|
| Model | Reolink E1 Pro |
| RTSP URL | `rtsp://admin:playervision2026@192.168.1.162:554/Preview_01_sub` |
| Stream | Sub stream (lower bandwidth, sufficient for stills) |
| Network | Local LAN (gym Wi-Fi) |

---

## 5. Supabase Database Schema

Run this SQL in the Supabase SQL Editor if you ever need to recreate the table:

```sql
create extension if not exists pgcrypto;

create table if not exists public.camera_stills (
  id uuid primary key default gen_random_uuid(),
  camera_id text not null,
  capture_slot bigint not null,
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

---

## 6. Configuration Reference

All settings live in `/etc/camera-pipeline/config.toml`. After any change, restart the service:

```bash
sudo systemctl restart camera-pipeline
```

### Capture interval

```toml
[capture]
interval_seconds = 30    # seconds between captures (default: 30)
```

Change to `60` for one capture per minute, `10` for every 10 seconds, etc. The slot-based idempotency adjusts automatically.

### Operating hours

```toml
[operating_hours]
timezone = "America/New_York"
start = "00:00"          # HH:MM in the configured timezone
end = "23:59"            # HH:MM — captures run while current time is >= start and < end
days = ["mon","tue","wed","thu","fri","sat","sun"]
```

**Currently set to 24/7.** To restrict to gym hours, change for example:

```toml
start = "06:00"
end = "22:00"
days = ["mon","tue","wed","thu","fri","sat"]
```

Supports midnight-crossing windows (e.g., `start = "22:00"`, `end = "02:00"`).

### JPEG quality and scaling

```toml
[capture]
jpeg_quality = 2         # FFmpeg -q:v value. Range 2-31. Lower = higher quality, larger file
scale_width = 0          # 0 = no scaling. Set to e.g. 1280 to downscale (aspect ratio preserved)
```

### Spool limits (backpressure)

```toml
[spool]
root_dir = "/var/lib/camera-pipeline"
max_pending_files = 20000      # stop capturing if this many jobs are queued
max_pending_gb = 10            # stop capturing if spool exceeds this size
delete_after_success = true    # remove local files after successful upload
```

### RTSP transport

```toml
[capture]
rtsp_transport = "tcp"           # "tcp" (reliable, recommended) or "udp"
ffmpeg_stimeout_us = 7000000     # FFmpeg RTSP timeout in microseconds (7 seconds)
```

### Supabase storage paths

```toml
[supabase]
prefix = "stills"                    # first path component in bucket
partition_timezone = "local"         # "local" = date folders in local tz, "utc" = UTC
storage_upsert = true                # allow overwriting same object key on retry
cache_control_seconds = 3600         # Cache-Control header on uploaded objects
```

---

## 7. Issues Encountered & Fixes Applied

### 1. FFmpeg `-stimeout` not recognized

**Problem:** The Pi's FFmpeg version doesn't support the deprecated `-stimeout` flag.
**Fix:** Changed to `-timeout` in `capture.py` line 43. This is the current FFmpeg flag for RTSP TCP I/O timeout (value in microseconds).

### 2. Environment variables not loaded during manual test

**Problem:** `sudo -u camera-pipeline -E` doesn't load variables from the `.env` file — `-E` only preserves the *calling user's* existing environment.
**Fix:** Source the env file explicitly before running:
```bash
sudo -u camera-pipeline bash -c 'set -a && source /etc/camera-pipeline/camera-pipeline.env && set +a && cd /opt/camera-pipeline && ...'
```
Note: This is a manual-testing issue only. The systemd service uses `EnvironmentFile=` which handles this automatically.

### 3. Env file permission denied

**Problem:** The env file was `chmod 600` owned by `root:root`, so the `camera-pipeline` user couldn't read it.
**Fix:** Changed ownership/permissions:
```bash
sudo chown root:camera-pipeline /etc/camera-pipeline/camera-pipeline.env
sudo chmod 640 /etc/camera-pipeline/camera-pipeline.env
```

### 4. Operating hours blocked midnight testing

**Problem:** Initial config had `start = "06:00"` / `end = "22:00"`, so the first test at midnight produced no captures (the 35-second `--once` window expired during the scheduler's sleep).
**Fix:** Widened to `start = "00:00"` / `end = "23:59"` for testing. Adjust to actual desired hours for production.

### 5. H.264 VUI overread warnings

**Symptom:** `[h264 @ 0x...] Overread VUI by 8 bits` in FFmpeg output.
**Impact:** None. This is a harmless warning from the camera's slightly non-standard H.264 stream metadata. Captures succeed normally.

---

## 8. Operating the Service

### Start / stop / restart

```bash
sudo systemctl start camera-pipeline
sudo systemctl stop camera-pipeline
sudo systemctl restart camera-pipeline
```

### Check status

```bash
sudo systemctl status camera-pipeline
```

### View live logs

```bash
journalctl -u camera-pipeline -f
```

### View health file

```bash
cat /var/lib/camera-pipeline/health.json
```

Returns JSON with `ts` (last check time), `pending_jobs` (queued count), `pending_bytes` (spool size).

### Enable on boot

```bash
sudo systemctl enable camera-pipeline
```

### Manual one-shot test

```bash
sudo -u camera-pipeline bash -c 'set -a && source /etc/camera-pipeline/camera-pipeline.env && set +a && cd /opt/camera-pipeline && /opt/camera-pipeline/venv/bin/python -m camera_pipeline.main --config /etc/camera-pipeline/config.toml --once'
```

---

## 9. Updating Code on the Pi

The repo lives on a laptop. To update the Pi after code changes:

1. Push changes to a git remote, then on the Pi:
   ```bash
   cd /opt/camera-pipeline && sudo git pull
   ```
   Or use `scp`/`rsync` to copy files directly.

2. If `requirements.txt` changed:
   ```bash
   sudo /opt/camera-pipeline/venv/bin/pip install -r /opt/camera-pipeline/requirements.txt
   ```

3. Restart the service:
   ```bash
   sudo systemctl restart camera-pipeline
   ```

---

## 10. Secrets Reference

These values are stored **only** in `/etc/camera-pipeline/camera-pipeline.env` on the Pi. They are **not** in version control.

| Variable | Source |
|----------|--------|
| `SUPABASE_URL` | Supabase dashboard → Project Settings → API |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase dashboard → Project Settings → API → service_role key |
| `CAMERA_RTSP_URL` | Reolink camera credentials + IP address |

The service role key bypasses Supabase RLS. Never expose it in client-side code or public repos.

---

## 11. Phase Two Extension Points

These are not implemented yet but the architecture supports them:

- **Continuous recording:** Use FFmpeg segment muxer to write 60s MP4 chunks to spool, upload similarly, index in a `camera_segments` table.
- **On-device inference:** Add a job runner that reads from the DB, downloads/reads the still, runs a model, writes results back.
- **Multi-camera:** Add more `[camera]` sections or run multiple service instances with different config files.
- **Admin UI:** Generate signed URLs server-side for viewing private bucket images (`create_signed_url` in Supabase SDK).

## Next Steps to Run Service

To run as a service:

`sudo cp /opt/camera-pipeline/deploy/camera-pipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-pipeline
journalctl -u camera-pipeline -f
`