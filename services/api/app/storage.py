from __future__ import annotations

import io
import os
import uuid

from minio import Minio

_BUCKET = "songbox-tracks"


def get_minio_client() -> Minio:
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    access_key = os.environ.get("MINIO_ACCESS_KEY", "songbox")
    secret_key = os.environ.get("MINIO_SECRET_KEY", "songbox-dev-only")
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)


def ensure_bucket(client: Minio) -> None:
    if not client.bucket_exists(_BUCKET):
        client.make_bucket(_BUCKET)


def save_track_file(client: Minio, tenant_id: uuid.UUID, data: bytes) -> str:
    """Storage key is bare tenant_id/uuid4 -- no client-supplied filename component at all, so
    nothing about the key is attacker-influenced (M1 originally appended the raw filename; that
    was flagged as an unnecessary risk and removed here)."""
    ensure_bucket(client)
    storage_key = f"{tenant_id}/{uuid.uuid4()}"
    client.put_object(_BUCKET, storage_key, io.BytesIO(data), length=len(data))
    return storage_key


def fetch_track_file(client: Minio, storage_key: str) -> bytes:
    response = client.get_object(_BUCKET, storage_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
