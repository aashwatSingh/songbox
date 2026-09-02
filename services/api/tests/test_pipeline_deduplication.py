"""Guards against a track's pipeline running twice.

A real track ended up with 8 stem rows (two complete sets), two transcriptions and two packages
because two chains were started for it: gpu_backend's _inference_lock only SERIALIZES heavy jobs,
so the second waited its turn and then redid all the work. Deduplication is a separate concern
from serialization, and these tests cover both halves of it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant, try_lock_track_pipeline
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.conftest import AuthedClient


def _upload_passed_track(client: TestClient, synthetic_wav: Path) -> str:
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


def _count_stems(tenant_id, track_id: str) -> int:
    session = db_session_for_tenant(tenant_id)
    try:
        return int(
            session.execute(
                text("SELECT count(*) FROM stems WHERE track_id = :tid"), {"tid": track_id}
            ).scalar_one()
        )
    finally:
        session.close()


def test_separate_twice_does_not_write_a_second_set_of_stems(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    """The exact shape of the observed bug: separation run twice, 8 stem rows instead of 4."""
    client = authed_client.client
    track_id = _upload_passed_track(client, synthetic_wav)

    first = client.post(f"/tracks/{track_id}/separate")
    assert first.status_code == 200
    assert _count_stems(authed_client.tenant_id, track_id) == 4

    second = client.post(f"/tracks/{track_id}/separate")

    assert second.status_code == 200
    assert _count_stems(authed_client.tenant_id, track_id) == 4, "a second set of stems was written"
    # And it reports the stems that actually exist, so a chained caller can keep going.
    assert {stem["stem_type"] for stem in second.json()["stems"]} == {
        stem["stem_type"] for stem in first.json()["stems"]
    }


def test_a_second_concurrent_job_is_refused_rather_than_queued(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    """While one request holds the per-track lock, another must fail fast with 409.

    Holding the lock in a separate open transaction is exactly what an in-flight request does; the
    point is that the second caller does NOT sit in the inference queue and then duplicate work.
    """
    client = authed_client.client
    track_id = _upload_passed_track(client, synthetic_wav)

    holder = db_session_for_tenant(authed_client.tenant_id)
    try:
        import uuid as _uuid

        assert try_lock_track_pipeline(holder, _uuid.UUID(track_id)) is True

        for stage in ("separate", "transcribe", "package"):
            response = client.post(f"/tracks/{track_id}/{stage}")
            assert response.status_code == 409, f"{stage} did not refuse a concurrent run"
            assert "already running" in response.json()["detail"]
    finally:
        # Rolling back releases the transaction-scoped advisory lock.
        holder.rollback()
        holder.close()

    # With the lock released the pipeline is usable again -- the guard must not be sticky.
    assert client.post(f"/tracks/{track_id}/separate").status_code == 200
