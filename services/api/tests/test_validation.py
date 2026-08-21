from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.validation import detect_audio_format


def _generate(tmp_path: Path, suffix: str, extra_args: list[str] | None = None) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH to run this test"
    out_path = tmp_path / f"tone{suffix}"
    args = [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if extra_args:
        args += extra_args
    args.append(str(out_path))
    result = subprocess.run(args, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return out_path.read_bytes()


def test_detects_wav(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".wav")) == "wav"


def test_detects_flac(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".flac")) == "flac"


def test_detects_mp3(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".mp3")) == "mp3"


def test_detects_m4a(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".m4a", ["-c:a", "aac"])) == "m4a"


def test_detects_ogg(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".ogg", ["-c:a", "libvorbis"])) == "ogg"


def test_detects_aiff(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".aiff")) == "aiff"


def test_rejects_truncated_header() -> None:
    assert detect_audio_format(b"RIFF") is None


def test_rejects_empty_bytes() -> None:
    assert detect_audio_format(b"") is None


def test_rejects_wrong_magic_bytes() -> None:
    assert detect_audio_format(b"this is definitely not an audio file, just plain text") is None


def test_rejects_playlist_with_remote_url() -> None:
    playlist = b"#EXTM3U\n#EXTINF:-1,Remote\nhttp://evil.example.com/payload.wav\n"
    assert detect_audio_format(playlist) is None
