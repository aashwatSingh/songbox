from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    # Rate-limit counters live in the real, persistent Redis instance (not an in-memory/mocked
    # store), so without this they'd carry over both across test functions within one run AND
    # across separate `pytest` invocations. Most test modules for the now-rate-limited routes
    # (test_tracks_upload.py, test_tracks_separate.py, test_tracks_transcribe.py,
    # test_tracks_realign.py, test_admin_takedown.py) construct `TestClient(app)` with Starlette's
    # default fixed client host ("testclient") rather than a unique IP per test -- their combined
    # call volume across a single `pytest` run exceeds the per-route limits and starts returning
    # 429 instead of the status codes they assert, unless each test starts from a clean bucket.
    # This resets ALL limiter state before every test, not just the rate-limiting tests', so
    # unrelated route tests stay accurate regardless of what ran before them. Limiter.reset() is
    # slowapi's own supported API for this (backed by limits' RedisStorage.reset()); it fires
    # before each test's actual requests run, so it never interferes with any single test's own
    # multi-request rate-limit assertions (e.g. test_rate_limiting.py's 21-requests-in-a-row
    # boundary checks), which all happen after this fixture has already run.
    limiter.reset()


@pytest.fixture
def synthetic_wav(tmp_path: Path) -> Path:
    """A tiny synthetic tone, generated fresh each test run -- not a real recording."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH to run this test"
    out_path = tmp_path / "tone.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return out_path


@pytest.fixture
def synthetic_wav_bytes(synthetic_wav: Path) -> bytes:
    return synthetic_wav.read_bytes()
