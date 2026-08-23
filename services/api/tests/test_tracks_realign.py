from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant
from app.main import app
from app.models import Transcription
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


def _insert_transcription(
    track_id: str,
    *,
    language: str = "en",
    lyrics_display_allowed: bool = True,
) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language=language,
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


def test_realign_stores_a_new_transcription_with_corrected_text(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 200
    body = response.json()
    assert [w["text"] for w in body["words"]] == ["hello", "world"]

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text(
                "SELECT whisper_model, aligner FROM transcriptions "
                "WHERE track_id = :track_id ORDER BY created_at"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 2
    assert rows[1].whisper_model == "user-corrected"
    assert rows[1].aligner == "wav2vec2"


def test_realign_rejects_track_with_no_transcription(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called with no transcription to correct")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 409


def test_realign_rejects_when_lyrics_display_not_allowed(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called when lyrics display isn't allowed")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, lyrics_display_allowed=False)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 409


def test_realign_rejects_non_english_tracks(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called for a non-English track")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, language="es")

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hola mundo"}
    )

    assert response.status_code == 409
