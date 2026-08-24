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
    # synthetic_wav is a pure sustained sine tone -- no attack transients for beat_track's
    # dynamic-programming beat picker to lock onto, so this exercises extract_structure's FALLBACK
    # tempo path specifically (librosa.feature.rhythm.tempo, per packaging.py's comment at that
    # call site), not beat_track's primary onset-based path. beats_ms is legitimately empty here.
    # See test_extract_structure_finds_beats_from_real_transients below for the primary path.
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


def test_extract_structure_finds_beats_from_real_transients(tmp_path: Path) -> None:
    # Every other test in this file (and every other test in this suite that reaches
    # extract_structure) uses synthetic_wav, a pure sustained sine tone with no rhythmic content --
    # so librosa.beat.beat_track's PRIMARY onset-based beat detection is never actually exercised
    # anywhere; beats_ms is always empty and tempo_bpm always comes from the fallback estimator.
    # Build a real "click track" -- periodic percussive noise bursts with a fast decay envelope,
    # the kind of audio beat_track's dynamic-programming beat picker is designed to lock onto -- to
    # genuinely exercise the primary path, with non-empty beats_ms as proof it ran.
    sample_rate = 44100
    bpm = 120.0
    beat_interval_s = 60.0 / bpm
    duration_s = 8.0
    num_samples = int(duration_s * sample_rate)
    rng = np.random.default_rng(seed=0)
    y = np.zeros(num_samples, dtype=np.float32)

    click_duration_s = 0.03
    click_samples = int(click_duration_s * sample_rate)
    decay = np.exp(-np.linspace(0, 12, click_samples)).astype(np.float32)
    beat_time = 0.0
    while beat_time < duration_s:
        start = int(beat_time * sample_rate)
        end = min(start + click_samples, num_samples)
        length = end - start
        y[start:end] += rng.standard_normal(length).astype(np.float32) * decay[:length]
        beat_time += beat_interval_s

    peak = float(np.abs(y).max())
    assert peak > 0
    y = y / peak * 0.9
    click_path = tmp_path / "click_track.wav"
    sf.write(str(click_path), np.stack([y, y], axis=1), sample_rate)

    result = extract_structure(click_path)

    assert len(result.beats_ms) > 0
    assert result.tempo_bpm > 0


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
