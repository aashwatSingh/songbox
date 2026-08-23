from __future__ import annotations

from pathlib import Path

import pytest

from app.transcription import (
    AlignmentError,
    _unflatten,
    align_words,
    run_transcription_and_alignment,
    transcribe_audio,
)


def test_transcribe_audio_returns_an_empty_transcript_when_no_speech_is_found(
    synthetic_wav: Path,
) -> None:
    # A pure sine tone has no speech at all -- faster-whisper finds zero segments for it. That
    # is a legitimate empty result (an instrumental-only track or a near-silent vocals stem after
    # separation are real, valid inputs too), not an error, so this must return a well-formed
    # empty Transcript rather than raising TranscriptionError.
    transcript = transcribe_audio(synthetic_wav)

    assert transcript.words == []
    assert transcript.text == ""
    assert transcript.language  # faster-whisper still identifies a language from the audio


def test_run_transcription_and_alignment_skips_alignment_when_no_speech_is_found(
    synthetic_wav: Path,
) -> None:
    # Aligning empty text is meaningless -- align_words() would just raise AlignmentError on it
    # -- so the orchestrator must skip alignment entirely and hand back an empty result labeled
    # "whisper_native" (trivially nothing for wav2vec2 to have changed) instead of raising.
    result = run_transcription_and_alignment(synthetic_wav, model_size="base")

    assert result.words == []
    assert result.aligner == "whisper_native"


def test_unflatten_groups_a_flat_list_by_given_lengths() -> None:
    flat = ["a", "b", "c", "d", "e"]
    grouped = _unflatten(flat, [2, 1, 2])
    assert grouped == [["a", "b"], ["c"], ["d", "e"]]


def test_align_words_produces_word_level_timings_in_order(synthetic_wav: Path) -> None:
    # A pure sine tone has no real speech, so wav2vec2 will align garbage confidently to
    # whatever text we give it -- this test proves the PIPELINE runs end-to-end and returns
    # well-formed, monotonically-ordered Word objects, not that the alignment is meaningful.
    words = align_words(synthetic_wav, "hello world")

    assert [w.text for w in words] == ["hello", "world"]
    assert [w.idx for w in words] == [0, 1]
    for word in words:
        assert word.start_ms >= 0
        assert word.end_ms >= word.start_ms
        assert 0.0 <= word.confidence <= 1.0
    # Word timings must be non-decreasing in start time -- forced_align guarantees monotonic
    # frame indices, so this should hold by construction; asserting it here catches a regression
    # in the frame-to-millisecond conversion, not just its presence.
    assert words[0].start_ms <= words[1].start_ms


def test_align_words_rejects_empty_text(synthetic_wav: Path) -> None:
    with pytest.raises(AlignmentError):
        align_words(synthetic_wav, "")


def test_align_words_handles_a_hyphenated_word(synthetic_wav: Path) -> None:
    # Regression test: the dictionary previously included the CTC blank ('-', index 0) alongside
    # every real label, so a literal hyphen in a word (e.g. "well-known") tokenized to include
    # index 0 and torchaudio.functional.forced_align(..., blank=0) rejected the whole target
    # tensor with "targets Tensor shouldn't contain blank index" -- crashing the entire request
    # for any track whose transcript contained a hyphenated word. This must now align cleanly.
    words = align_words(synthetic_wav, "a well-known song")

    assert [w.text for w in words] == ["a", "well-known", "song"]
    assert [w.idx for w in words] == [0, 1, 2]
    for word in words:
        assert word.start_ms >= 0
        assert word.end_ms >= word.start_ms
        assert 0.0 <= word.confidence <= 1.0


def test_align_words_drops_unalignable_words_instead_of_raising(synthetic_wav: Path) -> None:
    # Regression test: a word that reduces to zero alignable characters after tokenization (a
    # bare numeral like "1979", or the "♪" symbol Whisper sometimes emits for music passages)
    # previously raised AlignmentError and lost every other word's alignment along with it. The
    # alignable words in the same call must still come back with real timings, and the
    # unalignable ones must be cleanly excluded rather than crashing the whole call.
    words = align_words(synthetic_wav, "back in 1979")

    assert [w.text for w in words] == ["back", "in"]
    assert [w.idx for w in words] == [0, 1]
    for word in words:
        assert word.start_ms >= 0
        assert word.end_ms >= word.start_ms
        assert 0.0 <= word.confidence <= 1.0


def test_align_words_drops_a_music_note_symbol(synthetic_wav: Path) -> None:
    words = align_words(synthetic_wav, "la la ♪ la")

    assert [w.text for w in words] == ["la", "la", "la"]
    assert [w.idx for w in words] == [0, 1, 2]


def test_align_words_raises_when_no_word_is_alignable(synthetic_wav: Path) -> None:
    with pytest.raises(AlignmentError):
        align_words(synthetic_wav, "1979 ♪ 42")
