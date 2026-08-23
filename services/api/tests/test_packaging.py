from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from app.packaging import (
    build_package,
    extract_pitch,
    extract_structure,
    synthesize_accompaniment,
)


def test_synthesize_accompaniment_sums_and_normalizes(synthetic_wav: Path, tmp_path: Path) -> None:
    # The fixture's raw peak depends on the installed ffmpeg build's lavfi sine generator (this
    # machine's ffmpeg produces roughly -18 dBFS, not near full-scale), so first rescale a copy
    # close to full scale here rather than relying on that generator default. Summing three
    # copies of a near-full-scale tone reliably goes over amplitude, so the result must be
    # peak-normalized back to <= 1.0 -- this proves real summing + normalization ran, not a
    # pass-through of one input.
    data, sample_rate = sf.read(str(synthetic_wav), dtype="float32", always_2d=True)
    source_peak = float(np.abs(data).max())
    assert source_peak > 0
    loud_path = tmp_path / "loud_tone.wav"
    sf.write(str(loud_path), data * (0.9 / source_peak), sample_rate)

    out_path = synthesize_accompaniment(loud_path, loud_path, loud_path, tmp_path)

    assert out_path.exists()
    data, sample_rate = sf.read(str(out_path), dtype="float32", always_2d=True)
    assert sample_rate == 44100
    assert data.shape[1] == 2
    peak = abs(data).max()
    assert peak <= 1.0 + 1e-6
    assert peak > 0.9  # normalization brings the peak close to 1.0, not to near-silence


def test_extract_pitch_produces_well_formed_frames(synthetic_wav: Path) -> None:
    frames = extract_pitch(synthetic_wav, model="tiny")

    assert len(frames) > 0
    for frame in frames:
        assert frame.time_ms >= 0
        assert 0.0 <= frame.confidence <= 1.0
        if frame.hz is not None:
            assert frame.hz > 0
    # Frame times must be non-decreasing -- catches a regression in the hop-length/index-to-ms
    # conversion, not just its presence.
    times = [f.time_ms for f in frames]
    assert times == sorted(times)


def test_extract_structure_produces_well_formed_result(synthetic_wav: Path, tmp_path: Path) -> None:
    accompaniment_path = synthesize_accompaniment(
        synthetic_wav, synthetic_wav, synthetic_wav, tmp_path
    )

    result = extract_structure(accompaniment_path)

    assert result.tempo_bpm > 0
    assert all(b >= 0 for b in result.beats_ms)
    assert result.beats_ms == sorted(result.beats_ms)
    assert len(result.sections_ms) > 0
    assert result.sections_ms[0] == 0
    assert result.sections_ms == sorted(result.sections_ms)


def test_build_package_orchestrates_all_three_stages(synthetic_wav: Path) -> None:
    result = build_package(
        vocals_path=synthetic_wav,
        drums_path=synthetic_wav,
        bass_path=synthetic_wav,
        other_path=synthetic_wav,
        pitch_model="tiny",
    )

    assert result.pitch_model == "tiny"
    assert len(result.pitch) > 0
    assert result.tempo_bpm > 0
    assert len(result.sections_ms) > 0
