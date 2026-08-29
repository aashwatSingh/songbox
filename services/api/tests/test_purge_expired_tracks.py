from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import SessionLocal
from app.fingerprint import fingerprint_audio
from app.main import app
from app.models import RightsDeclaration, Track
from app.routes.tracks import get_acoustid_client
from scripts.purge_expired_tracks import purge_expired_tracks
from tests.test_tracks_upload import _make_tone

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_track(synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def _backdate_and_set_status(track_id: str, *, status: str, created_at: datetime) -> None:
    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        track.status = status
        declaration = session.get(RightsDeclaration, track.rights_declaration_id)
        assert declaration is not None
        declaration.created_at = created_at
        session.commit()
    finally:
        session.close()


def test_purge_deletes_old_rejected_track(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    old = datetime.now(UTC) - timedelta(days=31)
    _backdate_and_set_status(track_id, status="rejected", created_at=old)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is None
    finally:
        session.close()


def test_purge_does_not_delete_recent_pending_track(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    recent = datetime.now(UTC) - timedelta(days=5)
    _backdate_and_set_status(track_id, status="pending_review", created_at=recent)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is not None
    finally:
        session.close()


def test_purge_never_deletes_passed_tracks_regardless_of_age(synthetic_wav: Path) -> None:
    track_id = _upload_track(synthetic_wav)
    old = datetime.now(UTC) - timedelta(days=365)
    _backdate_and_set_status(track_id, status="passed", created_at=old)

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, uuid.UUID(track_id)) is not None
    finally:
        session.close()


def test_purge_deletes_supplementary_attestation_declaration_too(tmp_path: Path) -> None:
    """Regression test for the bug the final review found: confirm-attestation's "stronger
    attestation" RightsDeclaration row links to its track via RightsDeclaration.track_id (not
    Track.rights_declaration_id, which never repoints to it by design -- see the comment above
    the `stronger = RightsDeclaration(...)` call in confirm_attestation()). Before that link
    existed, purge_expired_tracks() had no way to find and delete this supplementary row,
    orphaning it forever -- even though it carries attestation_text/user_id/ip_address -- once its
    track was purged.
    """
    tone = _make_tone(tmp_path, frequency=349)
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
        assert upload_response.status_code == 200
        assert upload_response.json()["status"] == "pending_review"
        track_id = upload_response.json()["track_id"]

        exact_title = KNOWN_MATCH_RESULT.matches[0].release_title
        confirm_response = client.post(
            f"/tracks/{track_id}/confirm-attestation",
            headers=HEADERS,
            json={"release_name": exact_title},
        )
        assert confirm_response.status_code == 200

        resolve_response = client.post(
            f"/review-queue/{track_id}/resolve", headers=HEADERS, json={"approve": False}
        )
        assert resolve_response.status_code == 200
        assert resolve_response.json()["status"] == "rejected"
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    track_uuid = uuid.UUID(track_id)
    old = datetime.now(UTC) - timedelta(days=31)

    session = SessionLocal()
    try:
        track = session.get(Track, track_uuid)
        assert track is not None
        original_declaration_id = track.rights_declaration_id
        original_declaration = session.get(RightsDeclaration, original_declaration_id)
        assert original_declaration is not None
        original_declaration.created_at = old

        supplementary_before = (
            session.execute(
                select(RightsDeclaration).where(RightsDeclaration.track_id == track_uuid)
            )
            .scalars()
            .all()
        )
        assert len(supplementary_before) == 1

        session.commit()
    finally:
        session.close()

    purge_expired_tracks()

    session = SessionLocal()
    try:
        assert session.get(Track, track_uuid) is None
        assert session.get(RightsDeclaration, original_declaration_id) is None
        supplementary_after = (
            session.execute(
                select(RightsDeclaration).where(RightsDeclaration.track_id == track_uuid)
            )
            .scalars()
            .all()
        )
        assert supplementary_after == []
    finally:
        session.close()
