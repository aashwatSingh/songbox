from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    # Rate-limit counters live in the real, persistent Redis instance (not an in-memory/mocked
    # store), so without this they'd carry over both across test functions within one run AND
    # across separate `pytest` invocations. Most test modules for the now-rate-limited routes
    # (test_tracks_upload.py, test_tracks_separate.py, test_tracks_transcribe.py,
    # test_tracks_realign.py, test_admin_takedown.py) construct `TestClient(app)` with Starlette's
    # default fixed client host ("testclient") rather than a unique IP per test -- their combined
    # call volume across a single `pytest` run exceeds the per-route limits and starts returning
    # 429 instead of the status codes they assert, unless each test starts from a clean bucket.
    # This resets ALL limiter state before every test, not just the rate-limiting tests', so
    # unrelated route tests stay accurate regardless of what ran before them. Limiter.reset() is
    # slowapi's own supported API for this (backed by limits' RedisStorage.reset()); it fires
    # before each test's actual requests run, so it never interferes with any single test's own
    # multi-request rate-limit assertions (e.g. test_rate_limiting.py's 21-requests-in-a-row
    # boundary checks), which all happen after this fixture has already run.
    limiter.reset()


@pytest.fixture
def synthetic_wav(tmp_path: Path) -> Path:
    """A tiny synthetic tone, generated fresh each test run -- not a real recording."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH to run this test"
    out_path = tmp_path / "tone.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return out_path


@pytest.fixture
def synthetic_wav_bytes(synthetic_wav: Path) -> bytes:
    return synthetic_wav.read_bytes()


_TEST_PASSWORD = "correct horse battery staple"


@dataclass(frozen=True)
class AuthedClient:
    client: TestClient
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def sign_up(client: TestClient, *, email: str | None = None) -> AuthedClient:
    """Signs up a fresh real user on the given TestClient instance (mutating its cookie jar with
    the resulting session) and returns the real tenant_id/user_id -- the real-auth replacement for
    constructing an arbitrary X-Dev-Tenant-Id/X-Dev-User-Id headers dict. Most tests should use the
    `authed_client` fixture below instead of calling this directly; call this directly only when a
    test needs to control the TestClient itself (e.g. test_rate_limiting.py's per-test simulated
    peer IP) or needs more than one distinct identity in the same test (e.g. a second tenant to
    prove cross-tenant isolation).
    """
    if email is None:
        # NOT @example.test -- RFC 2606 reserves .test as a special-use TLD, and the
        # email-validator package pydantic's EmailStr delegates to permanently rejects it as
        # undeliverable regardless of configuration. Task 3's implementer discovered this the hard
        # way (every real signup call 422'd); .com is the correct choice for synthetic test emails
        # that must pass real EmailStr validation.
        email = f"{uuid.uuid4()}@example.com"
    response = client.post("/auth/signup", json={"email": email, "password": _TEST_PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    return AuthedClient(
        client=client,
        tenant_id=uuid.UUID(body["tenant_id"]),
        user_id=uuid.UUID(body["user_id"]),
    )


@pytest.fixture
def authed_client() -> AuthedClient:
    return sign_up(TestClient(app))
