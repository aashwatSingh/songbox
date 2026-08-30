from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.gpu_backend import run_package, run_realign, run_separate, run_transcribe
from app.transcription import Word


def test_run_separate_dispatches_locally_by_default(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav_bytes: bytes
) -> None:
    monkeypatch.delenv("GPU_BACKEND", raising=False)
    stems = run_separate(synthetic_wav_bytes, model_name="htdemucs", timeout_seconds=1800)
    assert set(stems) == {"vocals", "drums", "bass", "other"}
    for stem_bytes in stems.values():
        assert isinstance(stem_bytes, bytes)
        assert len(stem_bytes) > 0


def test_run_separate_dispatches_to_modal_backend_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when GPU_BACKEND=modal, the function dispatches to Modal instead of local.
    This test doesn't exercise real inference (no synthetic_wav_bytes needed) -- it just verifies
    dispatch routing with a mocked Modal Function."""
    monkeypatch.setenv("GPU_BACKEND", "modal")
    fake_result = {"vocals": b"v", "drums": b"d", "bass": b"b", "other": b"o"}
    fake_fn = MagicMock()
    fake_fn.remote.return_value = fake_result
    from_name = MagicMock(return_value=fake_fn)
    monkeypatch.setattr("modal.Function.from_name", from_name)

    result = run_separate(b"audio", model_name="htdemucs", timeout_seconds=1800)

    assert result == fake_result
    from_name.assert_called_once_with("songbox-gpu", "run_separate")


def test_run_transcribe_returns_a_real_result_for_synthetic_audio(
    synthetic_wav_bytes: bytes,
) -> None:
    result = run_transcribe(synthetic_wav_bytes, model_size="tiny", timeout_seconds=1800)
    assert result.language
    assert isinstance(result.words, list)


def test_run_realign_returns_word_timings(synthetic_wav_bytes: bytes) -> None:
    words = run_realign(synthetic_wav_bytes, text="la la la", timeout_seconds=1800)
    assert isinstance(words, list)
    assert all(isinstance(w, Word) for w in words)


def test_run_package_accepts_four_separate_stem_byte_strings(
    synthetic_wav_bytes: bytes,
) -> None:
    result = run_package(
        vocals_bytes=synthetic_wav_bytes,
        drums_bytes=synthetic_wav_bytes,
        bass_bytes=synthetic_wav_bytes,
        other_bytes=synthetic_wav_bytes,
        pitch_model="tiny",
        timeout_seconds=3600,
    )
    assert result.tempo_bpm > 0
