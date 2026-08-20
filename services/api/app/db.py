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

    Postgres's SET/SET LOCAL grammar does not accept bound parameters -- only literals --
    so a parameterized SET LOCAL raises a syntax error at the driver level. set_config()
    is a regular function call and does accept one; its third argument (true) gives it
    the same transaction-scoped "local" semantics SET LOCAL would have, readable back via
    current_setting() exactly the same way (see Task 3's RLS policies). This must be the
    first statement executed on the session -- SQLAlchemy's Session begins its transaction
    lazily on first execute(), so this call itself starts it.
    """
    session = SessionLocal()
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
    except Exception:
        session.close()
        raise
    return session
