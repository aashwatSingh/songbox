from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.test_tracks_upload import HEADERS, _make_tone

client = TestClient(app)


def test_confirm_attestation_moves_held_track_to_passed(tmp_path: Path) -> None:
    tone = _make_tone(tmp_path, frequency=523)
    known_fp = fingerprint_audio(tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with tone.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert upload_response.json()["status"] == "pending_review"
        track_id = upload_response.json()["track_id"]

        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "My Own Unreleased Demo"},
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "passed"


def test_confirm_attestation_404s_for_unknown_track() -> None:
    response = client.post(
        f"/tracks/{uuid.uuid4()}/confirm-attestation",
        headers=HEADERS,
        json={"release_name": "whatever"},
    )
    assert response.status_code == 404
