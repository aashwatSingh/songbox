"""Measures real word-onset alignment accuracy against JamendoLyrics Multi-Lang
(jamendolyrics/jamendolyrics on Hugging Face). Not a test -- run manually, paste its real output
into docs/BENCHMARKS.md.

Two measurements, matching the two production code paths in app/transcription.py's
run_transcription_and_alignment():

1. "aligned" (English only, primary -- this is what PLAN.md's +/-50ms criterion is about): force-
   align the KNOWN-CORRECT reference lyrics against the audio via align_words(). Predicted and
   reference word lists are identical in count and order by construction, since align_words() is
   given the reference text as its own alignment target, so predicted/reference word pairs are
   compared directly with no matching step needed.

2. "whisper_native" (non-English fallback, and a secondary number for English): run
   transcribe_audio() and compare ITS OWN predicted words against the ground truth. Whisper's
   transcript may not exactly match the reference lyrics (real recognition errors), so predicted
   and reference words are reconciled with a difflib sequence match before scoring -- unmatched
   words are excluded from the onset-error number, and the match rate is reported alongside it so
   a low match rate can't hide inside an artificially good number.

Rights handling (binding, not optional -- see docs/superpowers/specs/2026-08-21-alignment-engine-
design.md's licensing correction): most JamendoLyrics tracks are CC BY-NC-ND/SA, not rights-clean.
This script (a) skips any row whose license_type contains "ND", and (b) deletes every artifact
derived from a track's audio (temp source file, separated stems, alignment output) immediately
after that track is scored. Only the aggregate numbers this script prints may ever be committed --
never the audio, never any per-track derived file.
"""
from __future__ import annotations

import difflib
import shutil
import statistics
import tempfile
from pathlib import Path

from datasets import Audio, load_dataset

from app.gpu_backend import run_inference
from app.separation import separate_audio
from app.transcription import align_words, transcribe_audio

INFERENCE_TIMEOUT_SECONDS = 1800
ENGLISH_LANGUAGE_CODE = "en"
ONSET_TOLERANCE_MS = 50
_PUNCTUATION = ".,!?;:\"'"


def _normalize(word: str) -> str:
    return word.lower().strip(_PUNCTUATION)


def _score_aligned(vocals_path: Path, reference_words: list[dict[str, object]]) -> list[float]:
    reference_text = " ".join(str(w["text"]) for w in reference_words)
    predicted = run_inference(
        lambda: align_words(vocals_path, reference_text), timeout_seconds=INFERENCE_TIMEOUT_SECONDS
    )
    errors: list[float] = []
    for pred, ref in zip(predicted, reference_words, strict=True):
        ref_start_ms = float(ref["start"]) * 1000
        errors.append(abs(pred.start_ms - ref_start_ms))
    return errors


def _score_whisper_native(
    vocals_path: Path, reference_words: list[dict[str, object]], model_size: str
) -> tuple[list[float], float]:
    transcript = run_inference(
        lambda: transcribe_audio(vocals_path, model_size=model_size),
        timeout_seconds=INFERENCE_TIMEOUT_SECONDS,
    )
    reference_texts = [str(w["text"]) for w in reference_words]
    reference_starts_ms = [float(w["start"]) * 1000 for w in reference_words]

    predicted_norm = [_normalize(w.text) for w in transcript.words]
    reference_norm = [_normalize(t) for t in reference_texts]
    matcher = difflib.SequenceMatcher(None, predicted_norm, reference_norm, autojunk=False)

    errors: list[float] = []
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            predicted_word = transcript.words[block.a + offset]
            ref_start_ms = reference_starts_ms[block.b + offset]
            errors.append(abs(predicted_word.start_ms - ref_start_ms))
            matched += 1

    match_rate = matched / len(reference_words) if reference_words else 0.0
    return errors, match_rate


def _summarize(errors: list[float]) -> tuple[float, float] | tuple[None, None]:
    if not errors:
        return None, None
    median = statistics.median(errors)
    within_tolerance = sum(1 for e in errors if e <= ONSET_TOLERANCE_MS) / len(errors) * 100
    return median, within_tolerance


def main(whisper_model_size: str = "base") -> None:
    dataset = load_dataset("jamendolyrics/jamendolyrics", split="test")
    dataset = dataset.cast_column("audio", Audio(decode=False))

    aligned_errors_by_lang: dict[str, list[float]] = {}
    native_errors_by_lang: dict[str, list[float]] = {}
    native_match_rates: list[float] = []
    skipped_nd = 0
    scored = 0

    for row in dataset:
        license_type = str(row["license_type"])
        if "ND" in license_type:
            skipped_nd += 1
            continue

        language = str(row["language"])
        audio_field = row["audio"]
        reference_words = list(row["words"])

        with tempfile.TemporaryDirectory(prefix="songbox-eval-") as tmp_dir:
            source_path = Path(tmp_dir) / "source.mp3"
            # jamendolyrics/jamendolyrics stores audio as a local-cache file path, not embedded
            # bytes (row["audio"]["bytes"] is None for every row; row["audio"]["path"] points at
            # the mp3 already downloaded into the HF cache by load_dataset() above) -- this
            # differs from the brief's assumption of embedded bytes, verified empirically against
            # all 79 rows before this script was run for real. Handle both shapes so the script
            # still works if a future dataset revision embeds bytes directly.
            audio_bytes = audio_field.get("bytes")
            if audio_bytes is not None:
                source_path.write_bytes(audio_bytes)
            else:
                shutil.copyfile(audio_field["path"], source_path)

            # separate_audio() is called INSIDE this try/finally (not before it) so that its own
            # mkdtemp()'d stem directory is covered by the cleanup below even if separation raises
            # partway through -- previously the call sat outside the protected region, so a
            # partial failure there could leak that temp directory.
            stem_paths: dict[str, Path] | None = None
            try:
                stem_paths = run_inference(
                    lambda source_path=source_path: separate_audio(source_path),
                    timeout_seconds=INFERENCE_TIMEOUT_SECONDS,
                )
                vocals_path = stem_paths["vocals"]

                if language == ENGLISH_LANGUAGE_CODE:
                    aligned_errors = _score_aligned(vocals_path, reference_words)
                    aligned_errors_by_lang.setdefault(language, []).extend(aligned_errors)

                native_errors, match_rate = _score_whisper_native(
                    vocals_path, reference_words, whisper_model_size
                )
                native_errors_by_lang.setdefault(language, []).extend(native_errors)
                native_match_rates.append(match_rate)
                scored += 1
            finally:
                if stem_paths is not None:
                    shutil.rmtree(next(iter(stem_paths.values())).parent, ignore_errors=True)

    print(f"Scored {scored} tracks, skipped {skipped_nd} ND-licensed tracks.\n")

    print("=== Aligned (wav2vec2 forced alignment against reference lyrics, English only) ===")
    for language, errors in sorted(aligned_errors_by_lang.items()):
        median, within = _summarize(errors)
        print(
            f"  {language}: n={len(errors)} words, median error={median:.1f}ms, "
            f"within {ONSET_TOLERANCE_MS}ms={within:.1f}%"
        )

    print(
        f"\n=== Whisper-native (whisper_model_size={whisper_model_size!r}, "
        "matched against reference via difflib) ==="
    )
    for language, errors in sorted(native_errors_by_lang.items()):
        median, within = _summarize(errors)
        print(
            f"  {language}: n={len(errors)} words, median error={median:.1f}ms, "
            f"within {ONSET_TOLERANCE_MS}ms={within:.1f}%"
        )
    if native_match_rates:
        print(f"  mean match rate across tracks: {statistics.mean(native_match_rates) * 100:.1f}%")


if __name__ == "__main__":
    import sys

    size = sys.argv[1] if len(sys.argv) > 1 else "base"
    main(whisper_model_size=size)
