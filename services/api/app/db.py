from __future__ import annotations

import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://songbox:songbox@localhost:5433/songbox"
)

_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def get_engine() -> Engine:
    return _engine


def db_session_for_tenant(tenant_id: uuid.UUID) -> Session:
    """Open a session and set the RLS tenant context for its transaction.

    SET LOCAL only applies within the transaction it's issued in, so this must be the
    first statement executed on the session -- SQLAlchemy's Session begins its transaction
    lazily on first execute(), so this call itself starts it.
    """
    session = SessionLocal()
    session.execute(text("SET LOCAL app.tenant_id = :tenant_id"), {"tenant_id": str(tenant_id)})
    return session
