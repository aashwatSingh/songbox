from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.fingerprint import FingerprintError, fingerprint_audio


def test_fingerprint_audio_returns_value_and_duration(synthetic_wav: Path) -> None:
    result = fingerprint_audio(synthetic_wav)
    assert result.value
    assert 2.5 < result.duration_seconds < 3.5


def test_fingerprint_audio_is_deterministic_for_same_input(synthetic_wav: Path) -> None:
    first = fingerprint_audio(synthetic_wav)
    second = fingerprint_audio(synthetic_wav)
    assert first.value == second.value


def test_fingerprint_audio_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FingerprintError):
        fingerprint_audio(tmp_path / "does-not-exist.wav")


def test_fingerprint_audio_rejects_duration_exceeding_the_cap(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "too_long.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=721",
            "-ar",
            "8000",
            "-ac",
            "1",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with pytest.raises(FingerprintError, match="duration"):
        fingerprint_audio(out_path)


def test_fingerprint_audio_rejects_stream_count_exceeding_the_cap(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "multi_stream.m4a"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=1",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:a",
            "aac",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with pytest.raises(FingerprintError, match="stream"):
        fingerprint_audio(out_path)


def test_fingerprint_audio_raises_on_probe_timeout(
    synthetic_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FingerprintError):
        fingerprint_audio(synthetic_wav)
