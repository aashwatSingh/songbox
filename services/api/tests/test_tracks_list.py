from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.conftest import AuthedClient, sign_up


def _upload_and_pass_track(client: TestClient, synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    return response.json()["track_id"]


def test_list_tracks_returns_only_the_calling_tenants_tracks(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    other = sign_up(TestClient(app))
    track_id = _upload_and_pass_track(authed_client.client, synthetic_wav)

    response = authed_client.client.get("/tracks")
    other_response = other.client.get("/tracks")

    assert response.status_code == 200
    track_ids = {t["track_id"] for t in response.json()}
    assert track_id in track_ids
    assert other_response.json() == []


def test_list_tracks_reports_has_transcription_accurately(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)
    separate_response = client.post(f"/tracks/{track_id}/separate")
    assert separate_response.status_code == 200

    before = client.get("/tracks")
    before_entry = next(t for t in before.json() if t["track_id"] == track_id)
    assert before_entry["has_transcription"] is False

    transcribe_response = client.post(f"/tracks/{track_id}/transcribe")
    assert transcribe_response.status_code == 200

    after = client.get("/tracks")
    after_entry = next(t for t in after.json() if t["track_id"] == track_id)
    assert after_entry["has_transcription"] is True
