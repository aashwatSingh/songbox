from __future__ import annotations

import httpx
import pytest

from app.acoustid.client import FixtureAcoustIDClient, HTTPAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT, NO_MATCH_RESULT

SECRET_KEY = "super-secret-acoustid-key"


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


# AcoustID authenticates via a `client=<api key>` query parameter, so httpx's own exception
# strings embed the key. result.error is NOT internal-only -- it reaches the browser via
# UploadResponse.reason and is persisted to fingerprint_matches.acoustid_response -- so any
# path that puts str(exc) in there leaks the credential to users and to the database.
@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(
            httpx.HTTPStatusError(
                f"400 Bad Request for url 'https://api.acoustid.org/v2/lookup?client={SECRET_KEY}'",
                request=httpx.Request("GET", f"https://api.acoustid.org/v2/lookup?client={SECRET_KEY}"),
                response=httpx.Response(400),
            ),
            id="http-status-error",
        ),
        pytest.param(
            httpx.ConnectTimeout(
                f"timed out connecting to https://api.acoustid.org/v2/lookup?client={SECRET_KEY}"
            ),
            id="transport-error",
        ),
    ],
)
def test_lookup_error_never_leaks_the_api_key(monkeypatch, raised: Exception) -> None:
    def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise raised

    monkeypatch.setattr(httpx, "get", boom)
    result = HTTPAcoustIDClient(api_key=SECRET_KEY).lookup("fp", duration_seconds=180.0)

    assert result.error is not None
    assert SECRET_KEY not in result.error
    assert "client=" not in result.error
    assert not result.matched
