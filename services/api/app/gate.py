from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from app.acoustid.client import AcoustIDResult


def personal_mode_enabled() -> bool:
    """Whether this deployment is a single-user personal install.

    Songbox's gate is built for a multi-tenant service that accepts audio from strangers: it holds
    anything it cannot positively clear, which is the right default there and the wrong one on a
    laptop where the only user is the person who owns the files. On a personal install the hold has
    nobody to escalate to -- there is no second human to review it -- so every track stops dead.

    SONGBOX_PERSONAL_MODE=1 makes the gate record its findings and pass anyway. Read at call time
    rather than import time so tests and a running server can toggle it.

    Default OFF. It must stay off for any deployment serving more than one person: the gate is the
    only thing standing between that deployment and processing content nobody has cleared.
    """
    return os.environ.get("SONGBOX_PERSONAL_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


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

    # Personal-install escape hatch. The fingerprint lookup above still ran and its real result is
    # still written to fingerprint_matches, so the audit trail records what the gate SAW -- this
    # only changes what it DOES about it. The reason string says so explicitly, so a track cleared
    # this way is never mistaken for one that genuinely cleared the checks.
    if personal_mode_enabled():
        matched = acoustid_result.matches[0].release_title if acoustid_result.matches else None
        detail = f"fingerprint matched {matched!r}" if matched else "no fingerprint match"
        return GateDecision(
            outcome=GateOutcome.PASSED,
            resolution=FingerprintResolution.NO_MATCH,
            reason=(
                f"SONGBOX_PERSONAL_MODE is on: passing without enforcement ({detail}). "
                "Not a rights clearance -- single-user install only."
            ),
        )

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
