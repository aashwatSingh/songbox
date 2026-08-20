from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import Identity, get_identity

app = FastAPI()


@app.get("/whoami")
def whoami(identity: Identity = Depends(get_identity)) -> dict[str, str]:
    return {"tenant_id": str(identity.tenant_id), "user_id": str(identity.user_id)}


client = TestClient(app)


def test_missing_headers_returns_401() -> None:
    response = client.get("/whoami")
    assert response.status_code == 401


def test_valid_headers_returns_identity() -> None:
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    response = client.get(
        "/whoami",
        headers={"X-Dev-Tenant-Id": tenant_id, "X-Dev-User-Id": user_id},
    )
    assert response.status_code == 200
    assert response.json() == {"tenant_id": tenant_id, "user_id": user_id}


def test_invalid_uuid_returns_401() -> None:
    response = client.get(
        "/whoami", headers={"X-Dev-Tenant-Id": "not-a-uuid", "X-Dev-User-Id": "not-a-uuid"}
    )
    assert response.status_code == 401
