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

        # "A Commercial Release" is a genuine substring of the fixture's matched release
        # title ("A Commercial Release (fixture)"), so this reconciles.
        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "A Commercial Release"},
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "passed"
    assert confirm_response.json()["reconciled"] is True


def test_confirm_attestation_with_unrelated_release_name_stays_pending_review(
    tmp_path: Path,
) -> None:
    tone = _make_tone(tmp_path, frequency=784)
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

        # An unrelated made-up release name must NOT be enough to self-clear the hold --
        # this is the actual regression this finding is about.
        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "My Own Unreleased Demo"},
        )

        assert confirm_response.status_code == 200
        assert confirm_response.json()["status"] == "pending_review"
        assert confirm_response.json()["reconciled"] is False

        queue_response = client.get("/review-queue", headers=HEADERS)
        assert queue_response.status_code == 200
        ids = [item["track_id"] for item in queue_response.json()]
        assert track_id in ids
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)


def test_confirm_attestation_404s_for_unknown_track() -> None:
    response = client.post(
        f"/tracks/{uuid.uuid4()}/confirm-attestation",
        headers=HEADERS,
        json={"release_name": "whatever"},
    )
    assert response.status_code == 404
