from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def get_identity(
    x_dev_tenant_id: str | None = Header(default=None, alias="X-Dev-Tenant-Id"),
    x_dev_user_id: str | None = Header(default=None, alias="X-Dev-User-Id"),
) -> Identity:
    if not x_dev_tenant_id or not x_dev_user_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "X-Dev-Tenant-Id and X-Dev-User-Id headers are required (dev auth stub -- see "
                "docs/superpowers/specs/2026-08-19-rights-gate-design.md)"
            ),
        )
    try:
        return Identity(
            tenant_id=uuid.UUID(x_dev_tenant_id), user_id=uuid.UUID(x_dev_user_id)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=401, detail="Dev identity headers must be valid UUIDs"
        ) from exc


def require_admin_key(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="admin API key not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Key")
