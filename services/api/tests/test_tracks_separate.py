from __future__ import annotations

import tempfile
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.db import db_session_for_tenant
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import SeparateResponse, get_acoustid_client
from app.storage import fetch_track_file, get_minio_client
from tests.conftest import AuthedClient


def _upload_and_pass_track(client: TestClient, synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def test_separate_stores_four_stems_for_a_passed_track(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)

    response = client.post(f"/tracks/{track_id}/separate")

    assert response.status_code == 200
    body = response.json()
    assert body["track_id"] == track_id
    stem_types = {s["stem_type"] for s in body["stems"]}
    assert stem_types == {"vocals", "drums", "bass", "other"}

    # Fetch the ACTUAL bytes back from MinIO for every stem and verify they're real 44.1kHz
    # stereo WAV data -- mirrors what tests/test_separation.py already asserts for
    # separate_audio()'s direct output, but here proves the full upload round-trip (bucket,
    # storage key, byte-for-byte content) rather than just the DB rows and key prefix.
    minio_client = get_minio_client()
    for stem in body["stems"]:
        assert stem["storage_key"].startswith(f"{authed_client.tenant_id}/")
        stem_bytes = fetch_track_file(minio_client, stem["storage_key"])
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(stem_bytes)
            tmp_path = Path(tmp.name)
        try:
            with wave.open(str(tmp_path), "rb") as wav_file:
                assert wav_file.getframerate() == 44100, (
                    f"{stem['stem_type']} stem is not 44.1kHz"
                )
                assert wav_file.getnchannels() == 2, f"{stem['stem_type']} stem is not stereo"
        finally:
            tmp_path.unlink(missing_ok=True)

    session = db_session_for_tenant(authed_client.tenant_id)
    try:
        rows = session.execute(
            text("SELECT stem_type, model_name FROM stems WHERE track_id = :track_id"),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 4
    assert {row.model_name for row in rows} == {"htdemucs"}


def test_separate_rejects_track_that_has_not_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for a track that hasn't passed")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)

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

    response = client.post(f"/tracks/{track_id}/separate")

    assert response.status_code == 409


def test_separate_rejects_unknown_model_name(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client

    def _fail_if_called(*args: object, **kwargs: object) -> dict[str, Path]:
        raise AssertionError("separate_audio must not be called for an unrecognized model_name")

    monkeypatch.setattr("app.routes.tracks.separate_audio", _fail_if_called)
    track_id = _upload_and_pass_track(client, synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/separate",
        json={"model_name": "not-a-real-model"},
    )

    assert response.status_code == 422


def test_separate_returns_504_when_separation_exceeds_the_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    # Shrink the timeout to something the test can actually wait out, and make the (monkeypatched)
    # separate_audio() sleep past it -- this exercises the real Thread.join(timeout=...) path in
    # _separate_audio_with_timeout() without the test needing to wait anywhere near the real
    # SEPARATION_TIMEOUT_SECONDS (1800s) production value.
    client = authed_client.client
    monkeypatch.setattr("app.routes.tracks.SEPARATION_TIMEOUT_SECONDS", 0.05)

    def _slow_separate(*args: object, **kwargs: object) -> dict[str, Path]:
        time.sleep(0.5)
        return {}

    monkeypatch.setattr("app.routes.tracks.separate_audio", _slow_separate)
    track_id = _upload_and_pass_track(client, synthetic_wav)

    response = client.post(f"/tracks/{track_id}/separate")

    assert response.status_code == 504


def test_separate_commits_stems_before_returning(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    """The stems must be durable by the time the client sees 200, not merely flushed.

    FastAPI runs a yield-dependency's exit code (get_db's session.commit()) AFTER the response is
    sent. Relying on that teardown means /separate can answer 200 while its rows are still
    uncommitted, so a client that immediately chains POST /transcribe -- which the frontend does
    by design -- opens a new transaction, cannot see the vocals stem, and gets a spurious
    "track has no vocals stem -- run /separate first". That was an observed intermittent failure.

    Reading through a SEPARATE connection is what makes this a real test: the request's own
    session would happily show its own uncommitted rows either way.
    """
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)

    visible_to_other_connection: list[int] = []

    # Count the committed stem rows from an independent session at the moment the response is
    # built, i.e. before get_db's teardown could commit anything.
    original_response_cls = SeparateResponse

    def _spy(*args: object, **kwargs: object) -> object:
        other = db_session_for_tenant(authed_client.tenant_id)
        try:
            count = other.execute(
                text("SELECT count(*) FROM stems WHERE track_id = :tid"), {"tid": track_id}
            ).scalar_one()
        finally:
            other.close()
        visible_to_other_connection.append(int(count))
        return original_response_cls(*args, **kwargs)

    monkeypatch.setattr("app.routes.tracks.SeparateResponse", _spy)

    response = client.post(f"/tracks/{track_id}/separate")

    assert response.status_code == 200
    assert visible_to_other_connection == [4], (
        f"expected all 4 stems committed and visible to another connection before responding, "
        f"saw {visible_to_other_connection}"
    )
