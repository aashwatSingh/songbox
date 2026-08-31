from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import AppSessionLocal, SessionLocal
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
            email=f"{uuid.uuid4()}@example.com",
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


def test_songbox_app_role_has_no_grant_on_users() -> None:
    # Final-review finding #4: the two tests above prove users/sessions LACK an RLS policy --
    # that's the opposite of proving they're protected. The actual protection is the absence of
    # any Postgres GRANT to the restricted `songbox_app` role (see migration
    # 0009_add_users_and_sessions.py's comment and docs/adr/0002-authentication-model.md). This
    # opens a session as that restricted role (AppSessionLocal, not SessionLocal) and asserts a
    # plain SELECT against `users` is rejected at the database level, per CLAUDE.md's "add the
    # enforcing test the moment the first table lands" rule.
    session = AppSessionLocal()
    try:
        with pytest.raises(DBAPIError):
            session.execute(text("SELECT 1 FROM users"))
    finally:
        session.rollback()
        session.close()


def test_songbox_app_role_has_no_grant_on_sessions() -> None:
    session = AppSessionLocal()
    try:
        with pytest.raises(DBAPIError):
            session.execute(text("SELECT 1 FROM sessions"))
    finally:
        session.rollback()
        session.close()
