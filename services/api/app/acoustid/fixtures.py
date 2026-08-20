"""Canned AcoustID fixture data for tests. Not real AcoustID responses -- synthetic data shaped
like the real API's output, keyed at use-site by whatever fingerprint the test's own synthetic
audio actually produces (see tests/conftest.py's synthetic_wav fixture)."""

from __future__ import annotations

from app.acoustid.client import AcoustIDMatch, AcoustIDResult

KNOWN_MATCH_RESULT = AcoustIDResult(
    matches=[
        AcoustIDMatch(
            release_title="A Commercial Release (fixture)",
            recording_id="11111111-1111-1111-1111-111111111111",
            score=0.95,
        )
    ]
)

NO_MATCH_RESULT = AcoustIDResult(matches=[])

ERROR_RESULT = AcoustIDResult(matches=[], error="simulated AcoustID timeout")
