from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Header, HTTPException, Request
from sqlalchemy import select

from app.models import User, UserSession

SESSION_COOKIE_NAME = "songbox_session"
SESSION_TTL = timedelta(days=30)

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(user: User) -> str:
    """Creates a new sessions row for `user` and returns the RAW token to set as the cookie
    value -- only its sha256 hash is ever persisted (see _hash_token), so a database read alone
    can never produce a valid session cookie. Uses SessionLocal (the unrestricted `songbox` role),
    not AppSessionLocal -- sessions has no RLS policy and songbox_app has no grant on it (see
    alembic/versions/0009_add_users_and_sessions.py).
    """
    from app.db import SessionLocal

    raw_token = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        db.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=_hash_token(raw_token),
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + SESSION_TTL,
            )
        )
        db.commit()
    finally:
        db.close()
    return raw_token


def is_production() -> bool:
    return os.environ.get("SONGBOX_ENV", "development") == "production"


def get_identity(
    request: Request,
    songbox_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Identity:
    from app.db import SessionLocal

    if not songbox_session:
        raise HTTPException(status_code=401, detail="not signed in")

    token_hash = _hash_token(songbox_session)
    db = SessionLocal()
    try:
        session_row = db.execute(
            select(UserSession).where(UserSession.token_hash == token_hash)
        ).scalar_one_or_none()
        if session_row is None or session_row.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=401, detail="session expired or invalid")
        identity = Identity(tenant_id=session_row.tenant_id, user_id=session_row.user_id)
    finally:
        db.close()

    # Read by app/main.py's access-log middleware after call_next() returns -- a real, verified
    # tenant_id now, not an unverified client-supplied header (see main.py's log_requests).
    request.state.tenant_id = str(identity.tenant_id)
    return identity


def require_admin_key(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="admin API key not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Key")
