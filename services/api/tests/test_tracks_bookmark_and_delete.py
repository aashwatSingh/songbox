from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client
from tests.conftest import AuthedClient, sign_up


def _upload_and_pass_track(
    client: TestClient, synthetic_wav: Path, *, title: str | None = None, artist: str | None = None
) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        data = {"lane": "A", "attestation_text": "I made this recording"}
        if title is not None:
            data["title"] = title
        if artist is not None:
            data["artist"] = artist
        with synthetic_wav.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                data=data,
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    return response.json()["track_id"]


def test_upload_stores_title_and_artist(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(
        client, synthetic_wav, title="Golden Hour", artist="Mara Vale"
    )

    entry = next(t for t in client.get("/tracks").json() if t["track_id"] == track_id)
    assert entry["title"] == "Golden Hour"
    assert entry["artist"] == "Mara Vale"


def test_upload_without_title_or_artist_leaves_them_null(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)

    entry = next(t for t in client.get("/tracks").json() if t["track_id"] == track_id)
    assert entry["title"] is None
    assert entry["artist"] is None
    assert entry["bookmarked"] is False


def test_toggle_bookmark_flips_state_and_persists(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)

    on_response = client.post(f"/tracks/{track_id}/bookmark")
    assert on_response.status_code == 200
    assert on_response.json() == {"track_id": track_id, "bookmarked": True}

    entry = next(t for t in client.get("/tracks").json() if t["track_id"] == track_id)
    assert entry["bookmarked"] is True

    off_response = client.post(f"/tracks/{track_id}/bookmark")
    assert off_response.json()["bookmarked"] is False


def test_cannot_bookmark_another_tenants_track(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    other = sign_up(TestClient(app))
    track_id = _upload_and_pass_track(authed_client.client, synthetic_wav)

    response = other.client.post(f"/tracks/{track_id}/bookmark")

    assert response.status_code == 404


def test_bookmark_unknown_track_returns_404(authed_client: AuthedClient) -> None:
    response = authed_client.client.post(
        "/tracks/00000000-0000-0000-0000-000000000000/bookmark"
    )
    assert response.status_code == 404


def test_delete_track_removes_it_from_the_list(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)

    response = client.delete(f"/tracks/{track_id}")
    assert response.status_code == 204

    track_ids = {t["track_id"] for t in client.get("/tracks").json()}
    assert track_id not in track_ids


def test_deleted_track_is_genuinely_gone_not_just_unlisted(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    track_id = _upload_and_pass_track(client, synthetic_wav)
    client.post(f"/tracks/{track_id}/separate")

    delete_response = client.delete(f"/tracks/{track_id}")
    assert delete_response.status_code == 204

    # A second delete of the same (now-gone) track_id must 404, not succeed again -- proves the
    # row itself was removed, not just excluded from the list query.
    second_delete = client.delete(f"/tracks/{track_id}")
    assert second_delete.status_code == 404


def test_cannot_delete_another_tenants_track(
    synthetic_wav: Path, authed_client: AuthedClient
) -> None:
    other = sign_up(TestClient(app))
    track_id = _upload_and_pass_track(authed_client.client, synthetic_wav)

    response = other.client.delete(f"/tracks/{track_id}")
    assert response.status_code == 404

    # The original owner's track must still be there -- the other tenant's delete attempt must
    # not have silently succeeded against RLS-hidden state.
    track_ids = {t["track_id"] for t in authed_client.client.get("/tracks").json()}
    assert track_id in track_ids


def test_delete_unknown_track_returns_404(authed_client: AuthedClient) -> None:
    response = authed_client.client.delete("/tracks/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
