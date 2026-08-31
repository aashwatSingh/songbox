from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE_NAME,
    Identity,
    create_session,
    get_identity,
    hash_password,
    verify_password,
)
from app.db import SessionLocal
from app.models import User, UserSession

app = FastAPI()


@app.get("/whoami")
def whoami(identity: Identity = Depends(get_identity)) -> dict[str, str]:
    return {"tenant_id": str(identity.tenant_id), "user_id": str(identity.user_id)}


client = TestClient(app)


def _make_user() -> User:
    session = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            password_hash=hash_password("correct horse battery staple"),
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    finally:
        session.close()


def test_hash_password_produces_a_real_argon2_hash_not_the_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_the_correct_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "correct horse battery staple") is True


def test_verify_password_rejects_the_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "wrong password entirely") is False


def test_missing_cookie_returns_401() -> None:
    response = client.get("/whoami")
    assert response.status_code == 401


def test_garbage_cookie_returns_401() -> None:
    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: "not-a-real-token"})
    assert response.status_code == 401


def test_valid_session_returns_the_real_identity() -> None:
    user = _make_user()
    raw_token = create_session(user)

    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: raw_token})

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(user.tenant_id), "user_id": str(user.id)}


def test_expired_session_returns_401() -> None:
    user = _make_user()
    session = SessionLocal()
    try:
        raw_token = "expired-token-fixture"
        import hashlib

        session.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                created_at=datetime.now(UTC) - timedelta(days=31),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.get("/whoami", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 401


def test_create_session_never_persists_the_raw_token() -> None:
    user = _make_user()
    raw_token = create_session(user)

    session = SessionLocal()
    try:
        rows = session.query(UserSession).filter(UserSession.user_id == user.id).all()
    finally:
        session.close()

    assert len(rows) == 1
    assert rows[0].token_hash != raw_token
