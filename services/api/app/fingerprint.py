from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_DURATION_SECONDS = 12 * 60
MAX_STREAM_COUNT = 2
SUBPROCESS_TIMEOUT_SECONDS = 30


class FingerprintError(Exception):
    """Raised when ffmpeg/ffprobe cannot produce a fingerprint for the given file, or the file
    fails a hardening check (duration cap, stream-count cap, subprocess timeout)."""


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration_seconds: float


def fingerprint_audio(path: Path) -> Fingerprint:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise FingerprintError("ffmpeg/ffprobe not found on PATH")

    try:
        probe_result = subprocess.run(
            [
                ffprobe,
                "-protocol_whitelist",
                "file",
                "-v",
                "error",
                "-show_entries",
                "format=duration,nb_streams",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"ffprobe timed out after {exc.timeout}s") from exc

    if probe_result.returncode != 0 or not probe_result.stdout.strip():
        raise FingerprintError("ffprobe could not read the file")

    probe_values: dict[str, str] = {}
    for line in probe_result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        probe_values[key] = value

    try:
        duration_seconds = float(probe_values["duration"])
        stream_count = int(probe_values["nb_streams"])
    except (KeyError, ValueError) as exc:
        raise FingerprintError(
            f"ffprobe returned unparseable output: {probe_result.stdout!r}"
        ) from exc

    if duration_seconds > MAX_DURATION_SECONDS:
        raise FingerprintError(
            f"duration {duration_seconds:.1f}s exceeds the {MAX_DURATION_SECONDS}s limit"
        )
    if stream_count > MAX_STREAM_COUNT:
        raise FingerprintError(f"stream count {stream_count} exceeds the {MAX_STREAM_COUNT} limit")

    try:
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
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"ffmpeg timed out after {exc.timeout}s") from exc

    if fp_result.returncode != 0 or not fp_result.stdout.strip():
        raise FingerprintError("ffmpeg could not fingerprint the file")

    return Fingerprint(value=fp_result.stdout.decode().strip(), duration_seconds=duration_seconds)
