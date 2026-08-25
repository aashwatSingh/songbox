from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant
from app.karaoke_schema import KARAOKE_SCHEMA_V1
from app.main import app
from app.models import KaraokePackage, Transcription
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
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
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def _insert_transcription(track_id: str, *, lyrics_display_allowed: bool = True) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language="en",
                lyrics_display_allowed=lyrics_display_allowed,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
                    {"idx": 1, "text": "world", "start_ms": 400, "end_ms": 800, "confidence": 0.9},
                ],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


def _build_package(track_id: str) -> None:
    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)
    assert response.status_code == 200


def _insert_package(track_id: str, *, pitch_model: str, created_at: datetime) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            KaraokePackage(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                schema_version=1,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
                ],
                pitch_model=pitch_model,
                pitch=[{"time_ms": 0, "hz": 220.0, "confidence": 0.9}],
                tempo_bpm=120.0,
                beats_ms=[0, 500],
                sections_ms=[0],
                created_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()


def test_get_package_returns_a_schema_valid_document_and_working_stem_urls(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)
    _build_package(track_id)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    jsonschema.validate(instance=body["karaoke"], schema=KARAOKE_SCHEMA_V1)
    assert body["karaoke"]["track_id"] == track_id
    assert body["karaoke"]["pitch"]["model"] == "tiny"
    assert body["karaoke"]["pitch"]["hop_ms"] == 10
    assert len(body["karaoke"]["pitch"]["frames"]) > 0
    assert set(body["stem_urls"].keys()) == {"drums", "bass", "other"}

    for stem_type, path in body["stem_urls"].items():
        stem_response = client.get(path, headers=HEADERS)
        assert stem_response.status_code == 200, stem_type
        assert stem_response.headers["content-type"] == "audio/wav"
        assert len(stem_response.content) > 0


def test_get_package_rejects_unknown_track() -> None:
    response = client.get(f"/tracks/{uuid.uuid4()}/package", headers=HEADERS)
    assert response.status_code == 404


def test_get_package_returns_404_before_a_package_exists(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    # Separated, but /package was never called.

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 404


def test_get_package_nulls_word_text_when_lyrics_display_is_not_allowed(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, lyrics_display_allowed=False)
    _build_package(track_id)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    jsonschema.validate(instance=body["karaoke"], schema=KARAOKE_SCHEMA_V1)
    for word in body["karaoke"]["words"]:
        assert word["text"] is None


def test_get_package_returns_the_latest_package_when_multiple_exist(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    now = datetime.now(UTC)
    _insert_package(track_id, pitch_model="tiny", created_at=now - timedelta(minutes=5))
    _insert_package(track_id, pitch_model="full", created_at=now)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["karaoke"]["pitch"]["model"] == "full"


def test_get_stem_rejects_vocals_and_unknown_stem_types(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)
    _build_package(track_id)

    for stem_type in ("vocals", "master"):
        response = client.get(f"/tracks/{track_id}/stems/{stem_type}", headers=HEADERS)
        assert response.status_code == 404, stem_type


def test_get_stem_rejects_unknown_track() -> None:
    response = client.get(f"/tracks/{uuid.uuid4()}/stems/drums", headers=HEADERS)
    assert response.status_code == 404
