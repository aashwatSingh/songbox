from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal
from app.main import app
from app.models import RightsDeclaration, Track
from app.routes.tracks import get_acoustid_client
from scripts.purge_expired_tracks import purge_expired_tracks

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
