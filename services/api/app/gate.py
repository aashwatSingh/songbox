from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.acoustid.client import AcoustIDResult


class GateOutcome(StrEnum):
    PASSED = "passed"
    HELD = "pending_review"


class FingerprintResolution(StrEnum):
    NO_MATCH = "no_match"
    HELD = "held"
    CONFIRMED = "confirmed"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    resolution: FingerprintResolution
    reason: str


def resolve_lane_outcome(
    lane: str,
    acoustid_result: AcoustIDResult,
    license_covers_recording: bool | None = None,
) -> GateDecision:
    """Implements the lane x match-result table from
    docs/superpowers/specs/2026-08-19-rights-gate-design.md's Gate flow section."""

    if acoustid_result.error:
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason=f"AcoustID lookup failed ({acoustid_result.error}); holding for manual review",
        )

    if not acoustid_result.matched:
        return GateDecision(
            outcome=GateOutcome.PASSED,
            resolution=FingerprintResolution.NO_MATCH,
            reason="no fingerprint match found",
        )

    if lane == "A":
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason="fingerprint matched a commercial release; needs a confirming attestation",
        )

    if lane == "B":
        if license_covers_recording:
            return GateDecision(
                outcome=GateOutcome.PASSED,
                resolution=FingerprintResolution.CONFIRMED,
                reason="matched release is covered by the license on file",
            )
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.MISMATCH,
            reason="matched release is not covered by the license on file",
        )

    if lane == "C":
        return GateDecision(
            outcome=GateOutcome.HELD,
            resolution=FingerprintResolution.HELD,
            reason=(
                "fingerprint matched an existing recording; PD/CC claims always need manual "
                "verification on a match"
            ),
        )

    raise ValueError(f"unknown lane: {lane!r}")


def resolve_lyrics_display_allowed(lane: str, license_covers_lyrics: bool | None) -> bool:
    """Lyric display rights are tracked separately from recording rights (CLAUDE.md). Lane A
    (creator-owned) and Lane C (public domain / Creative Commons) always allow lyric display.
    Lane B (licensed) allows it only if the license on file explicitly covers lyrics -- a
    license that covers the recording but not the lyrics is a real, supported case, and missing
    lyric clearance is a supported degraded state (timings without text), not an error."""
    if lane in ("A", "C"):
        return True
    if lane == "B":
        return bool(license_covers_lyrics)
    raise ValueError(f"unknown lane: {lane!r}")
