from __future__ import annotations

from pathlib import Path

import pytest

from app.transcription import AlignmentError, _unflatten, align_words


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
