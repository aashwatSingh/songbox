from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal
from app.main import app
from app.models import Stem, Track
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}

TEST_ADMIN_KEY = "test-admin-key-for-pytest-only"


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def test_takedown_rejects_missing_admin_key(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("delete_track_content must not be called without a valid admin key")

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/admin/tracks/{track_id}/takedown", json={"reason": "test"})

    assert response.status_code == 401


def test_takedown_rejects_wrong_admin_key(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("delete_track_content must not be called with a wrong admin key")

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "test"},
        headers={"X-Admin-Key": "wrong-key"},
    )

    assert response.status_code == 401


def test_takedown_fails_closed_when_admin_key_not_configured(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "delete_track_content must not be called with no admin key configured"
        )

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "test"},
        headers={"X-Admin-Key": "anything"},
    )

    assert response.status_code == 500


def test_takedown_404s_for_unknown_track(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("delete_track_content must not be called for an unknown track")

    monkeypatch.setattr("app.routes.admin.delete_track_content", _fail_if_called)

    response = client.post(
        f"/admin/tracks/{uuid.uuid4()}/takedown",
        json={"reason": "test"},
        headers={"X-Admin-Key": TEST_ADMIN_KEY},
    )

    assert response.status_code == 404


def test_takedown_creates_tombstone_and_removes_content_across_tenants(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    # This request carries NO X-Dev-Tenant-Id at all -- proving the endpoint reaches across
    # tenants by design (via the admin key alone), not by accident.
    response = client.post(
        f"/admin/tracks/{track_id}/takedown",
        json={"reason": "rights holder request"},
        headers={"X-Admin-Key": TEST_ADMIN_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "taken_down"
    assert body["takedown_reason"] == "rights holder request"
    assert body["takedown_at"] is not None

    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        assert track.status == "taken_down"
        assert track.takedown_reason == "rights holder request"
        stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
        assert stems == []
    finally:
        session.close()
