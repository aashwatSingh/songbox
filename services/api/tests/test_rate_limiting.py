from __future__ import annotations

import random
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.conftest import sign_up


def _random_test_ip() -> str:
    # conftest.py's autouse `_reset_rate_limits` fixture already resets ALL limiter state before
    # every test, so this isn't covering for a missing flush. A fresh, effectively-unique IP per
    # test still earns its keep independently of that reset: it keeps each test's bucket
    # genuinely private (no risk of two tests racing the same key if run in parallel, and no
    # cross-test coupling if the reset fixture is ever narrowed or removed), and it makes each
    # test self-contained and readable on its own, the same reasoning this codebase's other tests
    # use fresh random UUIDs for tenant isolation.
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}"


def test_separate_is_rate_limited_to_20_per_hour_and_429_carries_retry_after() -> None:
    # A nonexistent track_id 404s immediately, before any real Demucs work -- the rate-limit
    # decorator still counts every attempt regardless of what the route body does, so this
    # exercises the real /separate route's real limit without running real GPU inference.
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)
    fake_track_id = uuid.uuid4()

    responses = [client.post(f"/tracks/{fake_track_id}/separate") for _ in range(21)]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429
    assert "retry-after" in responses[20].headers


def test_transcribe_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)
    fake_track_id = uuid.uuid4()

    responses = [client.post(f"/tracks/{fake_track_id}/transcribe") for _ in range(21)]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_realign_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/realign", json={"text": "whatever"})
        for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_package_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)
    fake_track_id = uuid.uuid4()

    responses = [client.post(f"/tracks/{fake_track_id}/package") for _ in range(21)]

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
    sign_up(client_a)
    sign_up(client_b)

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        for _ in range(30):
            with synthetic_wav.open("rb") as fh:
                response = client_a.post(
                    "/tracks/upload",
                    data={"lane": "A", "attestation_text": "I made this recording"},
                    files={"file": ("tone.wav", fh, "audio/wav")},
                )
            assert response.status_code == 200

        with synthetic_wav.open("rb") as fh:
            exhausted_response = client_a.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert exhausted_response.status_code == 429

        # A distinct IP gets its own fresh bucket -- proves the counter is genuinely per-IP,
        # not a global/shared limit.
        with synthetic_wav.open("rb") as fh:
            b_response = client_b.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert b_response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)


def test_unlimited_route_never_rate_limits() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)
    for _ in range(50):
        response = client.get("/tracks")
        assert response.status_code == 200


def test_separate_rate_limit_is_scoped_to_the_endpoint_not_the_literal_path() -> None:
    # Regression test for a real bug found in final review: slowapi's key_style defaults to
    # "url", scoping the counter to the literal request path -- including the track_id in it.
    # Without key_style="endpoint" on the Limiter, varying the track_id across requests would
    # give each one its own fresh 20/hour bucket, making the limit trivially bypassable. This
    # drives 21 requests with 21 DIFFERENT track_ids from the same IP and confirms the 21st
    # still 429s -- proving the limit is genuinely scoped per-endpoint, not per-path.
    client = TestClient(app, client=(_random_test_ip(), 1))
    sign_up(client)

    responses = [client.post(f"/tracks/{uuid.uuid4()}/separate") for _ in range(21)]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_takedown_rate_limit_is_scoped_to_the_endpoint_not_the_literal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", "the-real-key-for-this-test")
    client = TestClient(app, client=(_random_test_ip(), 1))

    responses = [
        client.post(
            f"/admin/tracks/{uuid.uuid4()}/takedown",
            json={"reason": "test"},
            headers={"X-Admin-Key": "definitely-the-wrong-key"},
        )
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert responses[10].status_code == 429


def test_signup_is_rate_limited_to_10_per_minute_and_429_carries_retry_after() -> None:
    # Final-review finding #2: signup is the highest-value brute-force/DoS target in the whole
    # system (unauthenticated, and every request pays argon2's memory-hard hashing cost) and was
    # the only mutating route with no @limiter.limit(...) at all. 11 rapid signups from one
    # simulated peer IP, each a genuinely distinct account so the first 10 succeed on their own
    # merits rather than being masked by a 409 -- the 11th must 429 regardless.
    client = TestClient(app, client=(_random_test_ip(), 1))

    responses = [
        client.post(
            "/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "hunter22ab"},
        )
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]] == [200] * 10
    assert responses[10].status_code == 429
    assert "retry-after" in responses[10].headers


def test_login_is_rate_limited_to_10_per_minute_and_429_carries_retry_after() -> None:
    # Same finding as above, for /auth/login -- repeatedly hitting one real account (wrong
    # password each time) must still 429 on the 11th attempt, independent of whether any
    # individual attempt succeeds or fails.
    signup_client = TestClient(app, client=(_random_test_ip(), 1))
    email = f"{uuid.uuid4()}@example.com"
    signup_response = signup_client.post(
        "/auth/signup", json={"email": email, "password": "hunter22ab"}
    )
    assert signup_response.status_code == 200

    login_client = TestClient(app, client=(_random_test_ip(), 1))
    responses = [
        login_client.post("/auth/login", json={"email": email, "password": "wrong-password"})
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert responses[10].status_code == 429
    assert "retry-after" in responses[10].headers
