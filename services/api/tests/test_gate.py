from __future__ import annotations

import pytest

from app.acoustid.fixtures import ERROR_RESULT, KNOWN_MATCH_RESULT, NO_MATCH_RESULT
from app.gate import (
    FingerprintResolution,
    GateOutcome,
    resolve_lane_outcome,
    resolve_lyrics_display_allowed,
)


def test_no_match_always_passes_regardless_of_lane() -> None:
    for lane in ("A", "B", "C"):
        decision = resolve_lane_outcome(lane, NO_MATCH_RESULT)
        assert decision.outcome == GateOutcome.PASSED
        assert decision.resolution == FingerprintResolution.NO_MATCH


def test_lane_a_match_always_holds() -> None:
    decision = resolve_lane_outcome("A", KNOWN_MATCH_RESULT)
    assert decision.outcome == GateOutcome.HELD


def test_lane_b_match_with_covering_license_passes() -> None:
    decision = resolve_lane_outcome("B", KNOWN_MATCH_RESULT, license_covers_recording=True)
    assert decision.outcome == GateOutcome.PASSED
    assert decision.resolution == FingerprintResolution.CONFIRMED


def test_lane_b_match_without_covering_license_holds() -> None:
    decision = resolve_lane_outcome("B", KNOWN_MATCH_RESULT, license_covers_recording=False)
    assert decision.outcome == GateOutcome.HELD
    assert decision.resolution == FingerprintResolution.MISMATCH


def test_lane_c_match_always_holds_even_though_it_might_be_legitimately_pd() -> None:
    decision = resolve_lane_outcome("C", KNOWN_MATCH_RESULT)
    assert decision.outcome == GateOutcome.HELD


def test_acoustid_error_holds_rather_than_passing_silently() -> None:
    for lane in ("A", "B", "C"):
        decision = resolve_lane_outcome(lane, ERROR_RESULT)
        assert (
            decision.outcome == GateOutcome.HELD
        ), "a flaky AcoustID call must never silently pass"


def test_lane_a_always_allows_lyric_display() -> None:
    assert resolve_lyrics_display_allowed("A", license_covers_lyrics=None) is True


def test_lane_c_always_allows_lyric_display() -> None:
    assert resolve_lyrics_display_allowed("C", license_covers_lyrics=None) is True


def test_lane_b_allows_lyric_display_only_when_license_covers_lyrics() -> None:
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=True) is True
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=False) is False
    assert resolve_lyrics_display_allowed("B", license_covers_lyrics=None) is False


def test_resolve_lyrics_display_allowed_rejects_unknown_lane() -> None:
    with pytest.raises(ValueError, match="unknown lane"):
        resolve_lyrics_display_allowed("Z", license_covers_lyrics=None)
