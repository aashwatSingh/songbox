from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import db_session_for_tenant
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client
from app.transcription import TranscriptionResult, Word

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_and_pass_track(synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    track_id = _upload_and_pass_track(synthetic_wav)
    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def test_transcribe_stores_transcription_and_marks_lyrics_display_allowed_for_lane_a(
    synthetic_wav: Path,
) -> None:
    # synthetic_wav is a pure sine tone -- it has no real speech, so Whisper legitimately finds
    # zero words for it (see app.transcription's "no speech detected is a legitimate empty
    # result" fix). This proves the pipeline *runs* end-to-end and storage is well-formed --
    # a real 200 response, a persisted Transcription row with the right fields, and a
    # well-formed (possibly empty) words list -- not that any particular word is recognized,
    # mirroring M3's own synthetic-fixture philosophy.
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    assert body["lyrics_display_allowed"] is True
    assert isinstance(body["words"], list)
    for word in body["words"]:
        assert word["text"] is not None
        assert word["start_ms"] >= 0
        assert word["end_ms"] >= word["start_ms"]

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text(
                "SELECT whisper_model, aligner, language FROM transcriptions "
                "WHERE track_id = :track_id"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 1
    assert rows[0].whisper_model == "base"
    assert rows[0].aligner == "whisper_native"
    assert rows[0].language


def test_transcribe_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)

    known_fp = fingerprint_audio(synthetic_wav)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert upload_response.json()["status"] == "pending_review"
    track_id = upload_response.json()["track_id"]

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 409


def test_transcribe_rejects_track_with_no_vocals_stem(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run when no vocals stem exists")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)
    track_id = _upload_and_pass_track(synthetic_wav)
    # Deliberately NOT calling /separate -- no vocals stem exists for this track.

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 409


def test_transcribe_rejects_unknown_model_size(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> TranscriptionResult:
        raise AssertionError("transcription must not run for an unrecognized model_size")

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/transcribe",
        headers=HEADERS,
        json={"model_size": "not-a-real-size"},
    )

    assert response.status_code == 422


def test_transcribe_withholds_text_but_keeps_timings_when_lyrics_not_allowed(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    fake_result = TranscriptionResult(
        text="hello world",
        language="en",
        aligner="wav2vec2",
        words=[
            Word(idx=0, text="hello", start_ms=0, end_ms=400, confidence=0.9),
            Word(idx=1, text="world", start_ms=400, end_ms=800, confidence=0.9),
        ],
    )
    monkeypatch.setattr(
        "app.routes.tracks.run_transcription_and_alignment", lambda *a, **k: fake_result
    )
    # Lane B with no license_covers_lyrics=True on file -> lyrics_display_allowed must be False.
    monkeypatch.setattr("app.routes.tracks.resolve_lyrics_display_allowed", lambda *a, **k: False)

    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["lyrics_display_allowed"] is False
    assert len(body["words"]) == 2
    for word in body["words"]:
        assert word["text"] is None
        assert word["start_ms"] is not None


def test_get_transcription_returns_the_stored_result(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    post_response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)
    assert post_response.status_code == 200

    get_response = client.get(f"/tracks/{track_id}/transcription", headers=HEADERS)

    assert get_response.status_code == 200
    assert get_response.json() == post_response.json()


def test_get_transcription_returns_404_when_none_exists(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.get(f"/tracks/{track_id}/transcription", headers=HEADERS)

    assert response.status_code == 404


def test_transcribe_returns_504_when_it_exceeds_the_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    monkeypatch.setattr("app.routes.tracks.TRANSCRIPTION_TIMEOUT_SECONDS", 0.05)

    def _slow(*args: object, **kwargs: object) -> TranscriptionResult:
        time.sleep(0.5)
        return TranscriptionResult(text="", language="en", aligner="wav2vec2", words=[])

    monkeypatch.setattr("app.routes.tracks.run_transcription_and_alignment", _slow)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)

    assert response.status_code == 504
