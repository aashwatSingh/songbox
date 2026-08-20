from __future__ import annotations

from app.acoustid.client import FixtureAcoustIDClient, HTTPAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT, NO_MATCH_RESULT


def test_fixture_client_returns_configured_match() -> None:
    client = FixtureAcoustIDClient({"fp-known-match": KNOWN_MATCH_RESULT})
    result = client.lookup("fp-known-match", duration_seconds=180.0)
    assert result.matched
    assert result.matches[0].release_title == "A Commercial Release (fixture)"


def test_fixture_client_returns_no_match_for_unknown_fingerprint() -> None:
    client = FixtureAcoustIDClient({"fp-known-match": KNOWN_MATCH_RESULT})
    result = client.lookup("fp-totally-unknown", duration_seconds=180.0)
    assert result == NO_MATCH_RESULT
    assert not result.matched


def test_http_client_without_api_key_returns_error(monkeypatch) -> None:
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    client = HTTPAcoustIDClient(api_key=None)
    result = client.lookup("some-fingerprint", duration_seconds=180.0)
    assert result.error == "ACOUSTID_API_KEY is not set"
    assert not result.matched
