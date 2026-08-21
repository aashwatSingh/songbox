from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import db_session_for_tenant
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_and_pass_track(synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def test_separate_stores_four_stems_for_a_passed_track(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    stem_types = {s["stem_type"] for s in body["stems"]}
    assert stem_types == {"vocals", "drums", "bass", "other"}
    for stem in body["stems"]:
        assert stem["storage_key"].startswith(f"{HEADERS['X-Dev-Tenant-Id']}/")

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text("SELECT stem_type, model_name FROM stems WHERE track_id = :track_id"),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 4
    assert {row.model_name for row in rows} == {"htdemucs"}


def test_separate_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)

    known_fp = fingerprint_audio(synthetic_wav)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert upload_response.json()["status"] == "pending_review"
    track_id = upload_response.json()["track_id"]

    response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)

    assert response.status_code == 409


def test_separate_rejects_unknown_model_name(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for an unrecognized model_name")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/separate",
        headers=HEADERS,
        json={"model_name": "not-a-real-model"},
    )

    assert response.status_code == 422
