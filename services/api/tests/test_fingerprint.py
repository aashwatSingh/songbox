from __future__ import annotations

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
