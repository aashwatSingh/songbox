from __future__ import annotations

import shutil
import tempfile
import wave
from pathlib import Path

import torch
from demucs.api import Separator
from demucs.audio import save_audio

EXPECTED_SAMPLE_RATE = 44100
EXPECTED_CHANNELS = 2
STEM_TYPES = ("vocals", "drums", "bass", "other")


class SeparationError(Exception):
    """Raised when Demucs cannot separate the given file, or its output fails the
    44.1kHz-stereo-WAV invariant every stage boundary in this codebase must assert."""


def separate_audio(path: Path, model_name: str = "htdemucs") -> dict[str, Path]:
    """Run Demucs source separation on `path`, returning a dict of stem_type -> temp WAV path
    for all four of STEM_TYPES. Uses Demucs' own segmented/overlap-crossfade mode (`split=True`,
    `overlap=0.25` -- the library's defaults, passed explicitly here to document that this is
    deliberate) so memory is bounded by segment length rather than track length. Runs on GPU
    when available, CPU otherwise -- CI and any machine without a CUDA-enabled torch build fall
    back to CPU automatically rather than erroring.
    """
    try:
        separator = Separator(
            model=model_name,
            device="cuda" if torch.cuda.is_available() else "cpu",
            split=True,
            overlap=0.25,
        )
    except Exception as exc:
        raise SeparationError(f"could not load model {model_name!r}: {exc}") from exc

    wrong_rate = separator.samplerate != EXPECTED_SAMPLE_RATE
    wrong_channels = separator.audio_channels != EXPECTED_CHANNELS
    if wrong_rate or wrong_channels:
        raise SeparationError(
            f"model {model_name!r} operates at {separator.samplerate}Hz/"
            f"{separator.audio_channels}ch, expected "
            f"{EXPECTED_SAMPLE_RATE}Hz/{EXPECTED_CHANNELS}ch"
        )

    try:
        _origin, separated = separator.separate_audio_file(path)
    except Exception as exc:
        raise SeparationError(f"separation failed: {exc}") from exc

    missing = set(STEM_TYPES) - set(separated)
    if missing:
        raise SeparationError(f"model {model_name!r} did not produce stems: {sorted(missing)}")

    out_dir = Path(tempfile.mkdtemp(prefix="songbox-stems-"))
    stem_paths: dict[str, Path] = {}
    try:
        for stem_type in STEM_TYPES:
            tensor = separated[stem_type]
            if tensor.shape[0] != EXPECTED_CHANNELS:
                raise SeparationError(
                    f"{stem_type} stem has {tensor.shape[0]} channels, "
                    f"expected {EXPECTED_CHANNELS}"
                )
            out_path = out_dir / f"{stem_type}.wav"
            save_audio(tensor, str(out_path), samplerate=separator.samplerate)

            # Reopen the file we just wrote and check its real on-disk properties, rather than
            # only trusting the in-memory tensor/separator metadata above -- this makes the
            # 44.1kHz-stereo-WAV guarantee CLAUDE.md requires at every stage boundary structural,
            # not inferred.
            with wave.open(str(out_path), "rb") as wav_file:
                actual_rate = wav_file.getframerate()
                actual_channels = wav_file.getnchannels()
            if actual_rate != EXPECTED_SAMPLE_RATE or actual_channels != EXPECTED_CHANNELS:
                raise SeparationError(
                    f"{stem_type} stem was written as {actual_rate}Hz/{actual_channels}ch, "
                    f"expected {EXPECTED_SAMPLE_RATE}Hz/{EXPECTED_CHANNELS}ch"
                )

            stem_paths[stem_type] = out_path
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    return stem_paths
