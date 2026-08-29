from __future__ import annotations

import random
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _random_test_ip() -> str:
    # A fresh, effectively-unique IP per test -- Redis-backed rate-limit state persists across
    # test runs (there is no flush fixture), so each test needs its own bucket to stay isolated.
    # Same reasoning this codebase's other tests use fresh random UUIDs for tenant isolation.
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}"


def test_separate_is_rate_limited_to_20_per_hour_and_429_carries_retry_after() -> None:
    # A nonexistent track_id 404s immediately, before any real Demucs work -- the rate-limit
    # decorator still counts every attempt regardless of what the route body does, so this
    # exercises the real /separate route's real limit without running real GPU inference.
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/separate", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429
    assert "retry-after" in responses[20].headers


def test_transcribe_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/transcribe", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_realign_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(
            f"/tracks/{fake_track_id}/realign", headers=HEADERS, json={"text": "whatever"}
        )
        for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_package_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/package", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_takedown_rate_limits_even_wrong_admin_key_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real point of this endpoint's limit: it must throttle repeated WRONG-key guesses, not
    # just successful calls -- require_admin_key alone allows unlimited guesses since it never
    # increments any counter. Verified during planning that a plain @limiter.limit() decorator on
    # this route would NOT catch this case (the failing dependency runs first and the decorated
    # function is never reached) -- app/routes/admin.py wires the limiter as a dependency ordered
    # BEFORE require_admin_key specifically so this test passes.
    monkeypatch.setenv("ADMIN_API_KEY", "the-real-key-for-this-test")
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(
            f"/admin/tracks/{fake_track_id}/takedown",
            json={"reason": "test"},
            headers={"X-Admin-Key": "definitely-the-wrong-key"},
        )
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert responses[10].status_code == 429


def test_upload_rate_limit_boundary_and_per_ip_isolation(synthetic_wav: Path) -> None:
    ip_a = _random_test_ip()
    ip_b = _random_test_ip()
    client_a = TestClient(app, client=(ip_a, 1))
    client_b = TestClient(app, client=(ip_b, 1))

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        for _ in range(30):
            with synthetic_wav.open("rb") as fh:
                response = client_a.post(
                    "/tracks/upload",
                    headers=HEADERS,
                    data={"lane": "A", "attestation_text": "I made this recording"},
                    files={"file": ("tone.wav", fh, "audio/wav")},
                )
            assert response.status_code == 200

        with synthetic_wav.open("rb") as fh:
            exhausted_response = client_a.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert exhausted_response.status_code == 429

        # A distinct IP gets its own fresh bucket -- proves the counter is genuinely per-IP,
        # not a global/shared limit.
        with synthetic_wav.open("rb") as fh:
            b_response = client_b.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert b_response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)


def test_unlimited_route_never_rate_limits() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    for _ in range(50):
        response = client.get("/tracks", headers=HEADERS)
        assert response.status_code == 200
