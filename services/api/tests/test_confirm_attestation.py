from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import SessionLocal
from app.fingerprint import fingerprint_audio
from app.main import app
from app.models import RightsDeclaration
from app.routes.tracks import get_acoustid_client
from tests.test_tracks_upload import HEADERS, _make_tone

client = TestClient(app)


def test_confirm_attestation_records_evidence_but_never_clears_the_hold(tmp_path: Path) -> None:
    """The actual regression this test guards: confirm-attestation must NEVER be sufficient
    on its own to pass a held track, no matter what release_name is submitted -- including
    the exact matched release title, which is the strongest possible self-service bypass
    attempt (an uploader can read matched_release straight off GET /review-queue and echo it
    back). Only a human calling /review-queue/{id}/resolve may clear a hold.
    """
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

        # Try the exact matched release title -- the strongest possible bypass attempt.
        exact_title = KNOWN_MATCH_RESULT.matches[0].release_title
        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": exact_title},
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "pending_review"

    queue_response = client.get("/review-queue", headers=HEADERS)
    assert queue_response.status_code == 200
    ids = [item["track_id"] for item in queue_response.json()]
    assert track_id in ids

    # Confirm the evidence was actually recorded, scoped tightly enough that this can't pass
    # vacuously off unrelated rows: this test's own tenant (HEADERS is a fresh random UUID
    # per test process) plus this exact release_name, which no other test in this file uses.
    tenant_id = uuid.UUID(HEADERS["X-Dev-Tenant-Id"])
    user_id = uuid.UUID(HEADERS["X-Dev-User-Id"])
    session = SessionLocal()
    try:
        rows = (
            session.query(RightsDeclaration)
            .filter(
                RightsDeclaration.tenant_id == tenant_id,
                RightsDeclaration.release_name == exact_title,
            )
            .all()
        )
    finally:
        session.close()
    assert len(rows) == 1, f"expected exactly one stronger attestation, found {len(rows)}"
    assert rows[0].user_id == user_id
    assert rows[0].lane == "A"
    assert exact_title in rows[0].attestation_text


def test_confirm_attestation_with_trivial_one_character_release_name_still_does_not_pass(
    tmp_path: Path,
) -> None:
    """Regression test for the specific exploit found in review: a one-character release_name
    like "a" defeated the old substring-matching approach. There's no matching logic left to
    defeat, but this pins the behavior explicitly.
    """
    tone = _make_tone(tmp_path, frequency=659)
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
        track_id = upload_response.json()["track_id"]

        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "a"},
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "pending_review"

    queue_response = client.get("/review-queue", headers=HEADERS)
    ids = [item["track_id"] for item in queue_response.json()]
    assert track_id in ids


def test_confirm_attestation_then_human_resolve_actually_clears_the_hold(tmp_path: Path) -> None:
    """The only real path off a Lane A hold: a human calling /review-queue/{id}/resolve.
    Confirm-attestation plus a subsequent resolve should end with the track passed.
    """
    tone = _make_tone(tmp_path, frequency=740)
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
        track_id = upload_response.json()["track_id"]

        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": "My Own Unreleased Demo, Actually The Matched Release"},
        )
        assert confirm_response.status_code == 200

        resolve_response = client.post(
            f"/review-queue/{track_id}/resolve", headers=HEADERS, json={"approve": True}
        )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert resolve_response.status_code == 200
    assert resolve_response.json()["status"] == "passed"


def test_confirm_attestation_404s_for_unknown_track() -> None:
    response = client.post(
        f"/tracks/{uuid.uuid4()}/confirm-attestation",
        headers=HEADERS,
        json={"release_name": "whatever"},
    )
    assert response.status_code == 404
