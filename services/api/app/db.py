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


# Namespace for the pipeline's advisory locks, so a track-id hash here can never collide with an
# advisory lock taken for some unrelated purpose. Arbitrary constant, must fit in a signed int32.
PIPELINE_LOCK_NAMESPACE = 0x50495045  # "PIPE"


def try_lock_track_pipeline(session: Session, track_id: uuid.UUID) -> bool:
    """Take a per-track lock for the life of this transaction. False if someone else holds it.

    gpu_backend's _inference_lock already stops two heavy jobs running at the same instant, but it
    only SERIALIZES them -- a second request for the same track waits its turn and then does the
    work all over again. That is how a real track ended up with 8 stem rows (two complete sets),
    two transcriptions and two packages: two chains were started, both passed the has_stems check
    while neither had finished, and both ran to completion.

    This deduplicates instead of queueing. pg_try_advisory_xact_lock never blocks -- it returns
    false immediately -- so the second caller fails fast with a clear 409 rather than sitting in
    the inference queue for minutes to produce redundant rows. The lock is transaction-scoped, so
    it is released on commit or rollback and cannot be leaked by a crashed request.

    hashtext() is 32-bit, so two different track ids could in principle collide and one would get
    a spurious "already running". That fails closed (a refused duplicate, never corruption) and is
    vastly less likely than the duplicate-run bug it prevents.
    """
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:tid))"),
            {"ns": PIPELINE_LOCK_NAMESPACE, "tid": str(track_id)},
        ).scalar_one()
    )


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
