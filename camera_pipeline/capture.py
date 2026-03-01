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
