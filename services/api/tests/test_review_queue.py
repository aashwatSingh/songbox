from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.conftest import AuthedClient
from tests.test_tracks_upload import _make_tone


def _upload_held_track(tmp_path: Path, frequency: int, client: TestClient) -> str:
    tone = _make_tone(tmp_path, frequency=frequency)
    known_fp = fingerprint_audio(tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with tone.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.json()["status"] == "pending_review"
    track_id: str = response.json()["track_id"]
    return track_id


def test_held_track_appears_in_review_queue(tmp_path: Path, authed_client: AuthedClient) -> None:
    client = authed_client.client
    track_id = _upload_held_track(tmp_path, frequency=659, client=client)
    response = client.get("/review-queue")
    assert response.status_code == 200
    ids = [item["track_id"] for item in response.json()]
    assert track_id in ids


def test_resolving_review_approve_passes_the_track(
    tmp_path: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_held_track(tmp_path, frequency=698, client=client)
    response = client.post(f"/review-queue/{track_id}/resolve", json={"approve": True})
    assert response.status_code == 200
    assert response.json()["status"] == "passed"


def test_resolving_review_reject_rejects_the_track(
    tmp_path: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_held_track(tmp_path, frequency=740, client=client)
    response = client.post(f"/review-queue/{track_id}/resolve", json={"approve": False})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
