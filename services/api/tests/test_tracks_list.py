from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}
OTHER_TENANT_HEADERS = {
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


def test_list_tracks_returns_only_the_calling_tenants_tracks(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.get("/tracks", headers=HEADERS)
    other_response = client.get("/tracks", headers=OTHER_TENANT_HEADERS)

    assert response.status_code == 200
    track_ids = {t["track_id"] for t in response.json()}
    assert track_id in track_ids
    assert other_response.json() == []


def test_list_tracks_reports_has_transcription_accurately(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)
    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200

    before = client.get("/tracks", headers=HEADERS)
    before_entry = next(t for t in before.json() if t["track_id"] == track_id)
    assert before_entry["has_transcription"] is False

    transcribe_response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)
    assert transcribe_response.status_code == 200

    after = client.get("/tracks", headers=HEADERS)
    after_entry = next(t for t in after.json() if t["track_id"] == track_id)
    assert after_entry["has_transcription"] is True
