from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class AcoustIDMatch:
    release_title: str
    recording_id: str
    score: float


@dataclass(frozen=True)
class AcoustIDResult:
    matches: list[AcoustIDMatch]
    error: str | None = None  # set when the lookup itself failed (timeout, 5xx, malformed)

    @property
    def matched(self) -> bool:
        return bool(self.matches)


class AcoustIDClient(Protocol):
    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult: ...


class HTTPAcoustIDClient:
    """Real AcoustID API client.

    Reads the API key from ACOUSTID_API_KEY (unset until one exists).
    """

    BASE_URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, api_key: str | None = None, timeout_seconds: float = 5.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("ACOUSTID_API_KEY")
        self._timeout_seconds = timeout_seconds

    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult:
        if not self._api_key:
            return AcoustIDResult(matches=[], error="ACOUSTID_API_KEY is not set")
        try:
            response = httpx.get(
                self.BASE_URL,
                params={
                    "client": self._api_key,
                    "fingerprint": fingerprint,
                    "duration": int(duration_seconds),
                    "meta": "recordings+releasegroups",
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        # Never let the exception's own string reach the caller. AcoustID authenticates via a
        # `client=<api key>` QUERY PARAMETER (its API has no header auth), so httpx's error
        # messages embed the full URL -- including the key. That string does not stay local: it
        # flows into GateDecision.reason -> UploadResponse.reason (returned to the browser) and
        # into fingerprint_matches.acoustid_response (persisted). Reporting the failure type and
        # status code is enough to debug with, and keeps the credential out of both.
        except httpx.HTTPStatusError as exc:
            return AcoustIDResult(
                matches=[], error=f"AcoustID returned HTTP {exc.response.status_code}"
            )
        except httpx.HTTPError as exc:
            return AcoustIDResult(
                matches=[], error=f"could not reach AcoustID ({type(exc).__name__})"
            )
        except ValueError:
            return AcoustIDResult(matches=[], error="AcoustID returned a malformed response")

        if data.get("status") != "ok":
            status = data.get("status")
            return AcoustIDResult(
                matches=[], error=f"AcoustID returned status={status}"
            )

        matches = [
            AcoustIDMatch(
                release_title=(recording.get("releasegroups") or [{}])[0].get("title", "unknown"),
                recording_id=recording.get("id", ""),
                score=result.get("score", 0.0),
            )
            for result in data.get("results", [])
            for recording in result.get("recordings", [])
        ]
        return AcoustIDResult(matches=matches)


class FixtureAcoustIDClient:
    """Test double: returns canned results keyed by exact fingerprint string."""

    def __init__(self, fixtures: dict[str, AcoustIDResult]) -> None:
        self._fixtures = fixtures

    def lookup(self, fingerprint: str, duration_seconds: float) -> AcoustIDResult:
        return self._fixtures.get(fingerprint, AcoustIDResult(matches=[]))
