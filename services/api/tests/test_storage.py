from __future__ import annotations

import uuid

from app.storage import fetch_track_file, get_minio_client, save_track_file


def test_save_track_file_round_trips_through_minio() -> None:
    client = get_minio_client()
    tenant_id = uuid.uuid4()
    data = b"not real audio, just test bytes"

    storage_key = save_track_file(client, tenant_id, data)

    assert storage_key.startswith(f"{tenant_id}/")
    response = client.get_object("songbox-tracks", storage_key)
    try:
        assert response.read() == data
    finally:
        response.close()
        response.release_conn()


def test_fetch_track_file_returns_the_bytes_that_were_saved() -> None:
    client = get_minio_client()
    tenant_id = uuid.uuid4()
    data = b"not real audio, just test bytes"

    storage_key = save_track_file(client, tenant_id, data)
    fetched = fetch_track_file(client, storage_key)

    assert fetched == data
