from __future__ import annotations

import shutil
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import torch
from demucs.api import Separator
from demucs.audio import save_audio

EXPECTED_SAMPLE_RATE = 44100
EXPECTED_CHANNELS = 2
STEM_TYPES = ("vocals", "drums", "bass", "other")


@lru_cache(maxsize=2)
def _load_separator(model_name: str, device: str) -> Separator:
    """Cache the loaded Demucs model across requests.

    Constructing a Separator re-reads the model weights every time. Measured on this machine:
    7.15s on a cold start, 1.54s warm (OS page cache) -- against ~14s of actual inference for a
    232s track, so up to a third of a short separation was spent reloading weights the previous
    request had already loaded. Same fix, and same reasoning, as transcription.py's
    _load_whisper_model.

    maxsize=2 rather than unbounded: ALLOWED_SEPARATION_MODELS has two entries (htdemucs,
    htdemucs_ft) and a resident model holds GPU memory between requests (peak measured at ~549MB,
    comfortably within an 8GB card). Two covers both allowed models without becoming a leak if
    more are added later.
    """
    # shifts=0, NOT the library default of 1. Demucs' `shifts` applies a RANDOM time shift (0-0.5s)
    # and averages over that many predictions -- so shifts=1 averages exactly one randomly-shifted
    # prediction, which is randomness with none of the averaging benefit. Measured on a 232s track:
    #
    #   shifts=1   12.94s / 11.18s   run-to-run difference  -22.1 dB   (audibly different stems)
    #   shifts=0   11.78s / 10.78s   run-to-run difference -221.7 dB   (numerically identical)
    #
    # So this is faster AND makes separation reproducible: the same input now yields the same
    # stems every time, which matters because /separate is idempotent by contract and because a
    # bug that only shows up in one stem run is otherwise impossible to reproduce. The quality
    # benefit of shift-averaging only exists at shifts>=2, which costs a full extra pass each.
    return Separator(
        model=model_name, device=device, split=True, overlap=0.25, shifts=0
    )


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
        separator = _load_separator(model_name, "cuda" if torch.cuda.is_available() else "cpu")
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

    def _write_and_verify(stem_type: str) -> tuple[str, Path]:
        tensor = separated[stem_type]
        if tensor.shape[0] != EXPECTED_CHANNELS:
            raise SeparationError(
                f"{stem_type} stem has {tensor.shape[0]} channels, expected {EXPECTED_CHANNELS}"
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
        return stem_type, out_path

    try:
        # The four stems are independent ~40MB WAV writes -- measured at 2.91s sequentially for a
        # 232s track, against ~14s of inference, so it is worth overlapping. Encoding and file I/O
        # both release the GIL, so threads genuinely overlap here rather than just interleaving.
        # Each worker still runs the SAME per-stem validation as before; parallelism changes the
        # order work happens in, not what is checked. An exception in any worker surfaces from
        # .result() and is handled by the existing cleanup below.
        with ThreadPoolExecutor(max_workers=len(STEM_TYPES)) as pool:
            for stem_type, out_path in pool.map(_write_and_verify, STEM_TYPES):
                stem_paths[stem_type] = out_path
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    return stem_paths
