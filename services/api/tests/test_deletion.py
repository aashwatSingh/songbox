from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from minio.error import S3Error
from sqlalchemy import select

from app.acoustid.client import FixtureAcoustIDClient
from app.db import SessionLocal, db_session_for_tenant
from app.deletion import delete_track_content
from app.main import app
from app.models import FingerprintMatch, KaraokePackage, Stem, Track, Transcription
from app.routes.tracks import get_acoustid_client
from app.storage import fetch_track_file, get_minio_client
from tests.conftest import AuthedClient


def _upload_pass_and_separate_track(client: TestClient, synthetic_wav: Path) -> str:
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
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate")
    assert separate_response.status_code == 200
    return track_id


def _insert_transcription_and_package(track_id: str, tenant_id: uuid.UUID) -> None:
    session = db_session_for_tenant(tenant_id)
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language="en",
                lyrics_display_allowed=True,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9}
                ],
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            KaraokePackage(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                track_id=uuid.UUID(track_id),
                schema_version=1,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9}
                ],
                pitch_model="tiny",
                pitch=[{"time_ms": 0, "hz": 220.0, "confidence": 0.9}],
                tempo_bpm=120.0,
                beats_ms=[0, 500],
                sections_ms=[0],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


def test_delete_track_content_removes_rows_and_storage_but_not_the_track(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_pass_and_separate_track(client, synthetic_wav)
    _insert_transcription_and_package(track_id, authed_client.tenant_id)

    minio_client = get_minio_client()
    session = SessionLocal()
    try:
        track = session.get(Track, uuid.UUID(track_id))
        assert track is not None
        stems = session.execute(select(Stem).where(Stem.track_id == track.id)).scalars().all()
        assert len(stems) == 4
        stem_keys = [s.storage_key for s in stems]
        track_key = track.storage_key
        fingerprint_matches = session.execute(
            select(FingerprintMatch).where(FingerprintMatch.track_id == track.id)
        ).scalars().all()
        assert len(fingerprint_matches) >= 1
        # Confirm the real objects exist before deletion (fetch raises if missing).
        for key in [*stem_keys, track_key]:
            fetch_track_file(minio_client, key)

        delete_track_content(session, track)
        session.commit()

        # The track row itself and its rights_declaration_id must survive -- deletion_content()
        # never touches Track or RightsDeclaration, callers decide that part.
        surviving_track = session.get(Track, uuid.UUID(track_id))
        assert surviving_track is not None
        assert surviving_track.rights_declaration_id == track.rights_declaration_id

        assert session.execute(
            select(Stem).where(Stem.track_id == track.id)
        ).scalars().all() == []
        assert session.execute(
            select(Transcription).where(Transcription.track_id == track.id)
        ).scalars().all() == []
        assert session.execute(
            select(KaraokePackage).where(KaraokePackage.track_id == track.id)
        ).scalars().all() == []
        assert session.execute(
            select(FingerprintMatch).where(FingerprintMatch.track_id == track.id)
        ).scalars().all() == []

        for key in [*stem_keys, track_key]:
            try:
                fetch_track_file(minio_client, key)
                raise AssertionError(f"expected {key} to be deleted from storage")
            except S3Error:
                pass
    finally:
        session.close()
