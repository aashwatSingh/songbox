from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FingerprintError(Exception):
    """Raised when ffmpeg/ffprobe cannot produce a fingerprint for the given file."""


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration_seconds: float


def fingerprint_audio(path: Path) -> Fingerprint:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise FingerprintError("ffmpeg/ffprobe not found on PATH")

    duration_result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if duration_result.returncode != 0 or not duration_result.stdout.strip():
        raise FingerprintError(f"ffprobe could not read duration: {duration_result.stderr.strip()}")
    duration_seconds = float(duration_result.stdout.strip())

    fp_result = subprocess.run(
        [
            ffmpeg,
            "-protocol_whitelist",
            "file",
            "-i",
            str(path),
            "-f",
            "chromaprint",
            "-fp_format",
            "base64",
            "-",
        ],
        capture_output=True,
    )
    if fp_result.returncode != 0 or not fp_result.stdout.strip():
        stderr_msg = fp_result.stderr.decode(errors='replace').strip()
        raise FingerprintError(f"ffmpeg could not fingerprint {path}: {stderr_msg}")

    return Fingerprint(value=fp_result.stdout.decode().strip(), duration_seconds=duration_seconds)
