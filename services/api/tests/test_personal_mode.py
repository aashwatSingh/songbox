"""SONGBOX_PERSONAL_MODE: the single-user escape hatch from the rights gate.

The gate holds anything it cannot positively clear. On a multi-tenant service that is correct. On
a one-person install it is a dead end -- there is no second human to escalate a hold to, so every
track stops at pending_review and nothing can ever be processed.

These tests pin BOTH halves: that the hatch works when asked for, and -- more importantly -- that
it stays shut when it is not.
"""

from __future__ import annotations

import pytest

from app.acoustid.client import AcoustIDMatch, AcoustIDResult
from app.gate import GateOutcome, personal_mode_enabled, resolve_lane_outcome

COMMERCIAL_MATCH = AcoustIDResult(
    matches=[AcoustIDMatch(release_title="A Commercial Release", recording_id="r1", score=0.99)]
)
LOOKUP_FAILED = AcoustIDResult(matches=[], error="ACOUSTID_API_KEY is not set")


def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SONGBOX_PERSONAL_MODE", raising=False)
    assert personal_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_recognises_the_usual_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SONGBOX_PERSONAL_MODE", value)
    assert personal_mode_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_anything_else_leaves_the_gate_enforcing(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A typo must fail CLOSED. Enforcing when the operator meant to disable is a nuisance;
    not enforcing when they meant to enforce is the failure that matters."""
    monkeypatch.setenv("SONGBOX_PERSONAL_MODE", value)
    assert personal_mode_enabled() is False


def test_a_commercial_match_is_still_held_when_the_hatch_is_shut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGBOX_PERSONAL_MODE", raising=False)
    decision = resolve_lane_outcome("A", COMMERCIAL_MATCH)
    assert decision.outcome is GateOutcome.HELD


def test_a_failed_lookup_is_still_held_when_the_hatch_is_shut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGBOX_PERSONAL_MODE", raising=False)
    assert resolve_lane_outcome("A", LOOKUP_FAILED).outcome is GateOutcome.HELD


@pytest.mark.parametrize("lane", ["A", "B", "C"])
@pytest.mark.parametrize("result", [COMMERCIAL_MATCH, LOOKUP_FAILED, AcoustIDResult(matches=[])])
def test_personal_mode_passes_every_lane_and_every_lookup_outcome(
    monkeypatch: pytest.MonkeyPatch, lane: str, result: AcoustIDResult
) -> None:
    """The point of the hatch: no track can be held, whatever the fingerprint says."""
    monkeypatch.setenv("SONGBOX_PERSONAL_MODE", "1")
    assert resolve_lane_outcome(lane, result).outcome is GateOutcome.PASSED


def test_the_reason_says_plainly_that_nothing_was_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track passed this way must never read like one that genuinely cleared the checks --
    the reason is stored on the upload response and in the audit trail."""
    monkeypatch.setenv("SONGBOX_PERSONAL_MODE", "1")
    reason = resolve_lane_outcome("A", COMMERCIAL_MATCH).reason

    assert "SONGBOX_PERSONAL_MODE" in reason
    assert "Not a rights clearance" in reason
    # The real finding is still reported, not swallowed.
    assert "A Commercial Release" in reason
