from __future__ import annotations

import os
import uuid
from collections.abc import Generator

from fastapi import Depends
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Identity, get_identity

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://songbox:songbox@localhost:5433/songbox"
)
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://songbox_app:songbox_app@localhost:5433/songbox"
)

_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

_app_engine = create_engine(APP_DATABASE_URL, pool_pre_ping=True)
AppSessionLocal = sessionmaker(bind=_app_engine, expire_on_commit=False)


def get_engine() -> Engine:
    return _engine


def db_session_for_tenant(tenant_id: uuid.UUID) -> Session:
    """Open a session and set the RLS tenant context for its transaction.

    Postgres's SET/SET LOCAL grammar does not accept bound parameters -- only literals --
    so a parameterized SET LOCAL raises a syntax error at the driver level. set_config()
    is a regular function call and does accept one; its third argument (true) gives it
    the same transaction-scoped "local" semantics SET LOCAL would have, readable back via
    current_setting() exactly the same way (see Task 3's RLS policies). This must be the
    first statement executed on the session -- SQLAlchemy's Session begins its transaction
    lazily on first execute(), so this call itself starts it.
    """
    session = AppSessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
    except Exception:
        session.close()
        raise
    return session


def get_db(identity: Identity = Depends(get_identity)) -> Generator[Session, None, None]:
    session = db_session_for_tenant(identity.tenant_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_admin_db() -> Generator[Session, None, None]:
    """Cross-tenant session using the unrestricted `songbox` superuser role, bypassing RLS. For
    operations that legitimately need to reach across tenants by design -- retention purge,
    takedown -- not for anything a normal per-request endpoint should ever use. Every route that
    depends on this MUST be gated behind something stronger than the dev-tenant-header identity
    scheme (see app.auth.require_admin_key), since it has no tenant boundary at all.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
