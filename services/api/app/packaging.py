from __future__ import annotations

import math
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import librosa
import librosa.feature.rhythm
import numpy as np
import soundfile as sf
import torch
import torchcrepe

# Same 44.1kHz-stereo-WAV invariant separation.py's EXPECTED_SAMPLE_RATE/EXPECTED_CHANNELS assert
# at its stage boundary (CLAUDE.md: "assert at every stage boundary"). Duplicated here rather than
# imported, matching this codebase's existing per-file-helper-duplication convention (see this
# module's _load_waveform docstring for the same choice made for a different helper).
EXPECTED_SAMPLE_RATE = 44100
EXPECTED_CHANNELS = 2

CREPE_HOP_MS = 10
# Voiced/unvoiced filtering cutoff for torchcrepe's periodicity score -- an unvalidated default,
# not a value tuned or measured against any labeled singing dataset. Treat it as a reasonable
# starting point, not a calibrated accuracy threshold.
CREPE_CONFIDENCE_THRESHOLD = 0.5

SECONDS_PER_SECTION_HEURISTIC = 20.0
MIN_SECTIONS = 4
MAX_SECTIONS = 16


class AccompanimentError(Exception):
    """Raised when the three non-vocal stems cannot be combined into an accompaniment track."""


class PitchExtractionError(Exception):
    """Raised when torchcrepe cannot extract a pitch contour from the given vocals audio."""


class StructureExtractionError(Exception):
    """Raised when librosa cannot extract a beat grid or structural boundaries from the given
    accompaniment audio."""


@dataclass(frozen=True)
class PitchFrame:
    time_ms: int
    hz: float | None
    confidence: float


@dataclass(frozen=True)
class StructureResult:
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


@dataclass(frozen=True)
class PackageResult:
    pitch_model: str
    pitch: list[PitchFrame]
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


def _load_waveform(path: Path) -> tuple[torch.Tensor, int]:
    # Same soundfile-based loader as transcription.py's _load_waveform, for the same reason: the
    # inputs here are always guaranteed-WAV (M3 stems, or this module's own WAV test fixtures),
    # never arbitrary uploaded formats, so torchaudio.load()'s TorchCodec/FFmpeg-full-shared-build
    # requirement is unnecessary machinery. Do not "fix" this back to torchaudio.load().
    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    waveform = torch.from_numpy(data.T).contiguous()
    return waveform, sample_rate


def _wav_format(path: Path) -> tuple[int, int]:
    """Return (sample_rate, channels) read structurally off a WAV file's real header, same
    approach as separation.py's post-write check."""
    with wave.open(str(path), "rb") as wav_file:
        return wav_file.getframerate(), wav_file.getnchannels()


def synthesize_accompaniment(
    drums_path: Path, bass_path: Path, other_path: Path, out_dir: Path
) -> Path:
    """Sum the three non-vocal stems into a single accompaniment WAV, peak-normalized to avoid
    clipping from combining three independently-mastered tracks. Transient artifact -- never
    written to MinIO, never given a Stem row.
    """
    try:
        arrays: list[np.ndarray] = []
        sample_rate: int | None = None
        for path in (drums_path, bass_path, other_path):
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            if sample_rate is None:
                sample_rate = sr
            elif sr != sample_rate:
                raise AccompanimentError(
                    f"stem sample rate mismatch: {path} is {sr}Hz, expected {sample_rate}Hz"
                )
            arrays.append(data)
    except AccompanimentError:
        raise
    except Exception as exc:
        raise AccompanimentError("could not load stem audio") from exc

    assert sample_rate is not None
    min_len = min(a.shape[0] for a in arrays)
    summed: np.ndarray = np.stack([a[:min_len] for a in arrays], axis=0).sum(axis=0)
    peak = float(np.abs(summed).max())
    if peak > 1.0:
        summed = summed / peak

    out_path = out_dir / "accompaniment.wav"
    try:
        sf.write(str(out_path), summed, sample_rate)
    except Exception as exc:
        raise AccompanimentError("could not write accompaniment audio") from exc

    # This function produces a NEW audio artifact (unlike _load_waveform's callers, which only
    # consume already-guaranteed-WAV stem input) -- CLAUDE.md requires the 44.1kHz-stereo-WAV
    # invariant asserted at every stage boundary. Reopen the file we just wrote and check its real
    # on-disk properties, rather than only trusting the in-memory sample_rate/array-shape values
    # that produced it, mirroring separation.py's EXPECTED_SAMPLE_RATE/EXPECTED_CHANNELS check.
    try:
        actual_rate, actual_channels = _wav_format(out_path)
    except Exception as exc:
        raise AccompanimentError("could not verify written accompaniment audio") from exc
    if actual_rate != EXPECTED_SAMPLE_RATE or actual_channels != EXPECTED_CHANNELS:
        raise AccompanimentError(
            f"accompaniment was written as {actual_rate}Hz/{actual_channels}ch, expected "
            f"{EXPECTED_SAMPLE_RATE}Hz/{EXPECTED_CHANNELS}ch"
        )
    return out_path


def extract_pitch(vocals_path: Path, model: str = "tiny") -> list[PitchFrame]:
    try:
        waveform, sample_rate = _load_waveform(vocals_path)
    except Exception as exc:
        raise PitchExtractionError("could not load vocals audio") from exc
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    hop_length = int(sample_rate * CREPE_HOP_MS / 1000)

    try:
        pitch, periodicity = torchcrepe.predict(
            waveform,
            sample_rate,
            hop_length=hop_length,
            model=model,
            return_periodicity=True,
            device=device,
        )
    except Exception as exc:
        raise PitchExtractionError("pitch extraction failed") from exc

    frames: list[PitchFrame] = []
    num_frames = pitch.shape[1]
    for i in range(num_frames):
        confidence = float(periodicity[0, i])
        # A NaN would serialize as the bare token `NaN` in JSON, which Postgres jsonb rejects
        # (unlike a native float8 column) -- surfacing as an unhandled 500 rather than a clean
        # result. Low-probability with real torchcrepe output, but cheap to guard both fields
        # that land in the same JSONB payload -- a NaN periodicity is exactly the condition that
        # also produces a NaN hz, so both must be handled, not just the one PitchFrame.hz already
        # being float | None made convenient.
        if math.isnan(confidence):
            confidence = 0.0
        hz = float(pitch[0, i]) if confidence >= CREPE_CONFIDENCE_THRESHOLD else None
        if hz is not None and math.isnan(hz):
            hz = None
        frames.append(PitchFrame(time_ms=i * CREPE_HOP_MS, hz=hz, confidence=confidence))
    return frames


def extract_structure(accompaniment_path: Path) -> StructureResult:
    try:
        data, sample_rate = sf.read(str(accompaniment_path), dtype="float32", always_2d=True)
    except Exception as exc:
        raise StructureExtractionError("could not load accompaniment audio") from exc
    y = data.mean(axis=1)

    try:
        tempo, beat_times = librosa.beat.beat_track(y=y, sr=sample_rate, units="time")
    except Exception as exc:
        raise StructureExtractionError("beat tracking failed") from exc
    beats_ms = [int(round(t * 1000)) for t in np.asarray(beat_times).reshape(-1)]
    tempo_bpm = float(np.asarray(tempo).reshape(-1)[0])
    # A NaN here would serialize as the bare token `NaN`, which Postgres jsonb rejects -- treat it
    # the same as beat_track's own documented "0 BPM, no onset strength detected" case below rather
    # than letting it slip past the `tempo_bpm <= 0` check (NaN <= 0 is False in Python).
    if math.isnan(tempo_bpm):
        tempo_bpm = 0.0
    if tempo_bpm <= 0:
        # librosa.beat.beat_track's own docs: "If no onset strength could be detected, the
        # beat tracker estimates 0 BPM and returns an empty list." That happens for audio with
        # no attack transients for its dynamic-programming beat picker to lock onto -- e.g. a
        # sustained tone with no rhythmic onsets, as in this module's synthetic test fixture,
        # or a quiet ambient intro in a real accompaniment track. Fall back to the standalone
        # autocorrelation-based tempo estimator -- but be honest about what it returns in this
        # specific case: when the onset envelope genuinely has no rhythmic content (as opposed to
        # merely weak beat_track peaks), librosa.feature.rhythm.tempo's Bayesian estimator is
        # dominated by its own internal tempo prior (empirically centered around ~120 BPM), not by
        # measured signal from this audio. A `tempo_bpm` produced via this fallback should be
        # treated as a low-confidence estimate, not a confirmed measurement -- unlike beats_ms,
        # which is left empty in this case rather than fabricated.
        try:
            fallback_tempo = librosa.feature.rhythm.tempo(y=y, sr=sample_rate)
            tempo_bpm = float(np.asarray(fallback_tempo).reshape(-1)[0])
            if math.isnan(tempo_bpm):
                tempo_bpm = 0.0
        except Exception as exc:
            raise StructureExtractionError("tempo estimation failed") from exc

    duration_seconds = len(y) / sample_rate
    # k is a tunable heuristic (roughly one boundary per SECONDS_PER_SECTION_HEURISTIC), not a
    # measured "correct" section count -- librosa.segment.agglomerative requires k as input, it
    # does not discover the number of sections from the audio. Boundary POSITIONS, given k, are
    # real chroma-similarity signal; boundary COUNT is not something this code claims to detect.
    k = max(
        MIN_SECTIONS,
        min(MAX_SECTIONS, round(duration_seconds / SECONDS_PER_SECTION_HEURISTIC)),
    )

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sample_rate)
        bounds = librosa.segment.agglomerative(chroma, k)
        bound_times = librosa.frames_to_time(bounds, sr=sample_rate)
    except Exception as exc:
        raise StructureExtractionError("structure segmentation failed") from exc
    sections_ms = [int(round(t * 1000)) for t in bound_times]

    return StructureResult(tempo_bpm=tempo_bpm, beats_ms=beats_ms, sections_ms=sections_ms)


def build_package(
    vocals_path: Path,
    drums_path: Path,
    bass_path: Path,
    other_path: Path,
    pitch_model: str = "tiny",
) -> PackageResult:
    """Orchestrates all three packaging stages, mirroring transcription.py's
    run_transcription_and_alignment -- the route calls this single function through one
    run_inference() lock acquisition rather than acquiring the lock three separate times.

    Accompaniment synthesis (and its stem-sample-rate validation) runs BEFORE pitch extraction,
    not after -- deliberately: extract_pitch is the slow stage (CREPE, 31.6s-587.2s depending on
    model per docs/BENCHMARKS.md's M5 section), while synthesize_accompaniment is cheap and is
    where a malformed-stems error (AccompanimentError) would surface. Running the cheap validation
    first means that error fails fast instead of only appearing after the full CREPE cost is paid.
    """
    with tempfile.TemporaryDirectory(prefix="songbox-accompaniment-") as tmp_dir:
        accompaniment_path = synthesize_accompaniment(
            drums_path, bass_path, other_path, Path(tmp_dir)
        )
        structure = extract_structure(accompaniment_path)
    pitch = extract_pitch(vocals_path, model=pitch_model)
    return PackageResult(
        pitch_model=pitch_model,
        pitch=pitch,
        tempo_bpm=structure.tempo_bpm,
        beats_ms=structure.beats_ms,
        sections_ms=structure.sections_ms,
    )
