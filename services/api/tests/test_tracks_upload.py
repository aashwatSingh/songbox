from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.acoustid.fixtures import KNOWN_MATCH_RESULT
from app.fingerprint import fingerprint_audio
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _make_tone(tmp_path: Path, frequency: int) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / f"tone-{frequency}.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0
    return out_path


@pytest.fixture
def commercial_tone(tmp_path: Path) -> Path:
    return _make_tone(tmp_path, frequency=440)


@pytest.fixture
def original_tone(tmp_path: Path) -> Path:
    return _make_tone(tmp_path, frequency=880)


def test_lane_a_upload_of_known_commercial_fingerprint_is_held(commercial_tone: Path) -> None:
    known_fp = fingerprint_audio(commercial_tone)
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient(
        {known_fp.value: KNOWN_MATCH_RESULT}
    )
    try:
        with commercial_tone.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    assert response.status_code == 200
    assert response.json()["status"] == "pending_review"


def test_lane_a_upload_of_original_recording_passes(original_tone: Path) -> None:
    # No entry for this fingerprint in the fixture client at all -> no match -> passes.
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with original_tone.open("rb") as fh:
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


def test_upload_rejects_truncated_header() -> None:
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("tone.wav", b"RIFF", "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "file does not match any accepted audio format"


def test_upload_rejects_wrong_magic_bytes() -> None:
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("tone.wav", b"this is plain text, not audio at all", "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "file does not match any accepted audio format"


def test_upload_rejects_playlist_with_remote_url() -> None:
    playlist = b"#EXTM3U\n#EXTINF:-1,Remote\nhttp://evil.example.com/payload.wav\n"
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("playlist.wav", playlist, "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "file does not match any accepted audio format"


def test_upload_rejects_duration_bomb(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "too_long.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=721",
            "-ar",
            "8000",
            "-ac",
            "1",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with out_path.open("rb") as fh:
        response = client.post(
            "/tracks/upload",
            headers=HEADERS,
            data={"lane": "A", "attestation_text": "I made this recording"},
            files={"file": ("tone.wav", fh, "audio/wav")},
        )
    assert response.status_code == 422
