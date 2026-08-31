from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db import SessionLocal
from app.models import User, UserSession


def test_users_and_sessions_tables_have_no_rls_policy() -> None:
    session = SessionLocal()
    try:
        rows = session.execute(
            text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname IN ('users', 'sessions')"
            )
        ).all()
    finally:
        session.close()

    found = {row.relname: row.relrowsecurity for row in rows}
    assert found == {"users": False, "sessions": False}


def test_a_user_and_session_row_can_be_inserted_and_read_back() -> None:
    session = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            password_hash="not-a-real-hash-just-schema-test",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()

        user_session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_hash=str(uuid.uuid4()),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(user_session)
        session.commit()

        session.refresh(user)
        session.refresh(user_session)
        assert user_session.user_id == user.id
        assert user_session.tenant_id == user.tenant_id
    finally:
        session.close()
