from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import db_session_for_tenant
from app.fingerprint import fingerprint_audio
from app.main import app
from app.models import Transcription
from app.routes.tracks import get_acoustid_client
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


def _insert_transcription(
    track_id: str, tenant_id: uuid.UUID, *, lyrics_display_allowed: bool = True
) -> None:
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


def test_package_stores_a_karaoke_package_with_real_pitch_and_structure(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_pass_and_separate_track(client, synthetic_wav)
    _insert_transcription(track_id, authed_client.tenant_id)

    response = client.post(f"/tracks/{track_id}/package")

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    assert body["schema_version"] == 1
    assert body["pitch_model"] == "tiny"
    assert body["tempo_bpm"] > 0
    assert len(body["sections_ms"]) > 0
    assert [w["text"] for w in body["words"]] == ["hello", "world"]

    session = db_session_for_tenant(authed_client.tenant_id)
    try:
        rows = session.execute(
            __import__("sqlalchemy").text(
                "SELECT schema_version, pitch_model FROM karaoke_packages "
                "WHERE track_id = :track_id"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 1
    assert rows[0].schema_version == 1
    assert rows[0].pitch_model == "tiny"


def test_package_nulls_word_text_when_lyrics_display_is_not_allowed(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_pass_and_separate_track(client, synthetic_wav)
    _insert_transcription(track_id, authed_client.tenant_id, lyrics_display_allowed=False)

    response = client.post(f"/tracks/{track_id}/package")

    assert response.status_code == 200
    body = response.json()
    assert len(body["words"]) == 2
    for word in body["words"]:
        assert word["text"] is None
        assert word["start_ms"] is not None
        assert word["end_ms"] is not None
        assert word["confidence"] is not None
        assert word["idx"] is not None

    session = db_session_for_tenant(authed_client.tenant_id)
    try:
        rows = session.execute(
            __import__("sqlalchemy").text(
                "SELECT words FROM karaoke_packages WHERE track_id = :track_id"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 1
    stored_words = rows[0].words
    assert len(stored_words) == 2
    for word in stored_words:
        assert word["text"] is None
        assert word["start_ms"] is not None
        assert word["end_ms"] is not None
        assert word["confidence"] is not None
        assert word["idx"] is not None


def test_package_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)

    # Mirrors test_tracks_transcribe.py's not-passed-gate test: a FixtureAcoustIDClient seeded
    # with a real KNOWN_MATCH_RESULT for this exact fingerprint makes Lane A hold on upload, so
    # track.status stays "pending_review" -- a genuinely not-passed track, not a
    # passed-but-unseparated one (that's test_package_rejects_track_missing_a_stem below).
    known_fp = fingerprint_audio(synthetic_wav)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert upload_response.json()["status"] == "pending_review"
    track_id = upload_response.json()["track_id"]

    response = client.post(f"/tracks/{track_id}/package")

    assert response.status_code == 409


def test_package_rejects_track_missing_a_stem(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called when stems are missing")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    track_id = upload_response.json()["track_id"]
    # Not separated -- no stems exist at all.

    response = client.post(f"/tracks/{track_id}/package")

    assert response.status_code == 409


def test_package_rejects_track_with_no_transcription(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called with no transcription to embed")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)
    track_id = _upload_pass_and_separate_track(client, synthetic_wav)
    # Separated, but /transcribe was never called.

    response = client.post(f"/tracks/{track_id}/package")

    assert response.status_code == 409


def test_package_rejects_unknown_pitch_model(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_package must not be called for an unrecognized pitch_model")

    monkeypatch.setattr("app.routes.tracks.build_package", _fail_if_called)
    track_id = _upload_pass_and_separate_track(client, synthetic_wav)
    _insert_transcription(track_id, authed_client.tenant_id)

    response = client.post(
        f"/tracks/{track_id}/package",
        json={"pitch_model": "not-a-real-model"},
    )

    assert response.status_code == 422
